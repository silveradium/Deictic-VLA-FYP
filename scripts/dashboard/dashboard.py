"""Live dashboard for demos: what Roxy sees, hears, decides and logs, in a browser.

  make demo-site        starts it with the rest (http://localhost:8080)
  make dashboard        just this, next to a stack that is already running

Three tabs (static/index.html):
  Live   for viewers: the Orbbec stream, what was heard, where it went (Gemini or NaVILA), the decision
  VLA    every NaVILA request: the 8 frames actually sent, the model's text, the action, what the robot did
  Logs   readable logs per machine: laptop and Pi from /rosout, the NaVILA server from its /health and
         the requests

Sources: orbbec/color/image_raw/compressed (served as MJPEG, never decoded), stt/transcript, stt/request,
voice/qa (Gemini's intent + reply), voice/speaking, navila/step (navila_node, JSON), odom (rate only),
/rosout, and NAVILA_SERVER_URL/health. The page gets events over Server-Sent Events (/events) and a
snapshot of recent history on load (/state).
"""

import argparse
import json
import os
import queue
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import rclpy
from nav_msgs.msg import Odometry
from rcl_interfaces.msg import Log
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import Bool, String

STATIC = Path(__file__).parent / "static"
CAMERA = "orbbec/color/image_raw/compressed"
RATE_WINDOW = 3.0  # s
# /rosout node names that run on the Pi (robot/pi); everything else runs on the bringup laptop
PI_NODES = ("orbbec", "speaker_node", "camera")
# repeated noise that only hides the useful lines
NOISE = ("Failed to load schemaDefinition", "Failed to add service", "got segment", "Advertising new channel",
         "Removing channel", "Advertising new service", "Removing service")
LEVELS = {10: "debug", 20: "info", 30: "warn", 40: "error", 50: "fatal"}
LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
ROSOUT = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1000, reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL)


class Hub:
    """Fans events out to every open page, and keeps recent history for pages opened later."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()
        self.history = {"heard": deque(maxlen=30), "route": deque(maxlen=30), "step": deque(maxlen=40),
                        "log": deque(maxlen=1500)}

    def publish(self, kind, data, keep=True):
        event = {**data, "ev": kind}  # "ev", not "kind": NaVILA decisions carry their own "kind"
        with self.lock:
            if keep and kind in self.history:
                self.history[kind].append(event)
            for q in list(self.clients):
                try:
                    q.put_nowait(event)
                except queue.Full:  # a stalled page: drop it rather than block ROS
                    self.clients.discard(q)

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def snapshot(self):
        with self.lock:
            return {k: list(v) for k, v in self.history.items()}


class Dashboard(Node):
    def __init__(self, hub, server_url, motion):
        super().__init__("dashboard")
        self.hub, self.server_url, self.motion = hub, server_url.rstrip("/"), motion
        self.jpeg, self.jpeg_cond = None, threading.Condition()
        self.recent = {"camera": deque(), "odom": deque()}
        self.speaking = False
        self.server = {"ok": False, "error": "not checked yet"}
        self.up = {"camera": None, "odom": None}  # last reported state, for plain-language status lines

        self.create_subscription(CompressedImage, CAMERA, self.on_camera, qos_profile_sensor_data)
        self.create_subscription(Odometry, "odom", lambda _: self.tick("odom"), 10)
        self.create_subscription(String, "stt/transcript", self.on_transcript, 10)
        self.create_subscription(String, "stt/request", self.on_request, 10)
        self.create_subscription(String, "voice/qa", self.on_qa, 10)
        self.create_subscription(Bool, "voice/speaking", self.on_speaking, LATCHED)
        self.create_subscription(String, "navila/step", self.on_step, 50)
        self.create_subscription(Log, "/rosout", self.on_log, ROSOUT)
        self.create_timer(1.0, self.push_status)
        threading.Thread(target=self.poll_server, daemon=True).start()

    # ---------------- inputs ----------------
    def tick(self, name):
        q, now = self.recent[name], time.time()
        q.append(now)
        while q and q[0] < now - RATE_WINDOW:
            q.popleft()

    def rate(self, name):
        q, now = self.recent[name], time.time()
        while q and q[0] < now - RATE_WINDOW:
            q.popleft()
        return round(len(q) / RATE_WINDOW, 1)

    def on_camera(self, msg):
        self.tick("camera")
        with self.jpeg_cond:
            self.jpeg = bytes(msg.data)
            self.jpeg_cond.notify_all()

    def on_transcript(self, msg):
        if msg.data.strip():
            self.hub.publish("heard", {"t": time.time(), "text": msg.data.strip(), "wake": False})

    def on_request(self, msg):
        # wake_router: what followed "Hey Roxy" (empty = only the wake phrase)
        self.hub.publish("heard", {"t": time.time(), "text": msg.data.strip(), "wake": True})

    def on_qa(self, msg):
        try:
            qa = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        target = {"navigate": "navila", "question": "gemini"}.get(qa.get("intent"), "offline")
        self.hub.publish("route", {"t": time.time(), "heard": qa.get("heard", ""), "target": target,
                                   "reply": qa.get("reply", ""), "question": qa.get("question", "")})

    def on_speaking(self, msg):
        self.speaking = bool(msg.data)

    def on_step(self, msg):
        try:
            step = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        self.hub.publish("step", step)
        # the NaVILA server's own story for the Logs tab, from what the laptop sees of it
        kind = step.get("type")
        if kind == "query":
            self.server_log("info", f"request #{step.get('n')}: 8 frames + {step.get('instruction')!r}")
        elif kind == "decision":
            self.server_log("info", f"#{step.get('n')} {step.get('text')!r} -> {step.get('action')}  "
                                    f"(model {step.get('latency_s')} s, round trip {step.get('roundtrip_s')} s)")
        elif kind == "error":
            self.server_log("error", f"request failed: {step.get('error')}")

    def on_log(self, msg):
        text = msg.msg.strip()
        if not text or any(n in text for n in NOISE):
            return
        name = msg.name or "?"
        machine = "pi" if name.startswith(PI_NODES) or name.split(".")[-1].startswith(PI_NODES) else "laptop"
        t = msg.stamp.sec + msg.stamp.nanosec * 1e-9
        self.hub.publish("log", {"t": t, "machine": machine, "level": LEVELS.get(msg.level, "info"),
                                 "node": name, "text": text})

    def server_log(self, level, text):
        self.hub.publish("log", {"t": time.time(), "machine": "server", "level": level,
                                 "node": "navila_server", "text": text})

    # ---------------- outputs ----------------
    def poll_server(self):
        last_ok = None
        while rclpy.ok():
            try:
                t0 = time.time()
                with urllib.request.urlopen(f"{self.server_url}/health", timeout=5) as r:
                    info = json.loads(r.read())
                self.server = {"ok": bool(info.get("ok")), "ms": round((time.time() - t0) * 1000), **info}
            except Exception as e:  # unreachable, tunnel closed, model still loading
                self.server = {"ok": False, "error": str(e)[:120]}
            if self.server["ok"] != last_ok:
                if self.server["ok"]:
                    self.server_log("info", f"server up at {self.server_url}: {self.server.get('model')} "
                                            f"({self.server.get('precision')}, {self.server.get('vram_gib')} GiB VRAM)")
                else:
                    self.server_log("error", f"server unreachable at {self.server_url}: {self.server['error']}")
                last_ok = self.server["ok"]
            time.sleep(3.0)

    def watch_links(self):
        """One readable line when the camera stream or the robot's odometry starts or stops."""
        for name, machine, node, what in (("camera", "pi", "orbbec", "Orbbec camera stream"),
                                          ("odom", "laptop", "go2_driver_node", "robot odometry")):
            rate = self.rate(name)
            up = rate > 0
            if up != self.up[name]:
                if up:
                    self.hub.publish("log", {"t": time.time(), "machine": machine, "level": "info", "node": node,
                                             "text": f"{what} arriving at the laptop"})
                elif self.up[name] is not None:
                    self.hub.publish("log", {"t": time.time(), "machine": machine, "level": "warn", "node": node,
                                             "text": f"{what} stopped arriving (Wi-Fi? robot connection?)"})
                self.up[name] = up

    def push_status(self):
        self.watch_links()
        self.hub.publish("status", {"camera_fps": self.rate("camera"), "odom_hz": self.rate("odom"),
                                    "speaking": self.speaking, "server": self.server, "motion": self.motion,
                                    "server_url": self.server_url}, keep=False)


def make_handler(node, hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_bytes(self, data, ctype, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/stream.mjpg":
                return self.stream()
            if path == "/events":
                return self.events()
            if path == "/state":
                return self.send_bytes(json.dumps(hub.snapshot()).encode(), "application/json")
            name = "index.html" if path == "/" else path.lstrip("/")
            target = (STATIC / name).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self.send_bytes(b"not found", "text/plain", 404)
            ctype = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
                     ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".svg": "image/svg+xml", ".webp": "image/webp"}.get(target.suffix.lower(),
                                                                          "application/octet-stream")
            self.send_bytes(target.read_bytes(), ctype)

        def stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while True:
                    with node.jpeg_cond:
                        node.jpeg_cond.wait_for(lambda: node.jpeg is not last, timeout=2.0)
                        jpeg = node.jpeg
                    if jpeg is None or jpeg is last:
                        continue
                    last = jpeg
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = hub.subscribe()
            try:
                while True:
                    try:
                        event = q.get(timeout=10.0)
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", 8080)))
    args = ap.parse_args()
    rclpy.init()
    hub = Hub()
    node = Dashboard(hub, os.environ.get("NAVILA_SERVER_URL", "http://127.0.0.1:8000"),
                     os.environ.get("NAVILA_MOTION", "false").lower() == "true")
    threading.Thread(target=rclpy.spin, args=(node,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(node, hub))
    server.daemon_threads = True
    print(f"dashboard on http://localhost:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
