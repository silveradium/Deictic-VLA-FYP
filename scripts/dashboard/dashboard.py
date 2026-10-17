"""Live dashboard for demos: what Roxy sees, hears, decides and logs, in a browser.

  make demo-site        starts it with the rest (http://localhost:8080)
  make dashboard        just this, next to a stack that is already running

Four tabs (static/index.html):
  Live   for viewers: the Orbbec stream, what was heard, where it went (Gemini or NaVILA), the decision,
         and a text box for when the hall is too loud for the mic (stt/typed, wake_router)
  VLA    every NaVILA request: the 8 frames actually sent, the model's text, the action, what the robot did
  Logs   readable logs per machine: laptop and Pi from /rosout, the NaVILA server from its /health and
         the requests
  Voice  Roxy's voice lab: 100 robot voices (ros_ws/src/voice/voice/voice_library.py) and knobs for
         pitch, speed and effects, played here in the browser or on Roxy's speaker. "Use as Roxy's
         voice" sends it to tts_node (voice/config) and keeps it in data/voice/roxy_voice.json

Sources: the camera (DASHBOARD_CAMERA, set from CAMERA=go2|orbbec by the Makefile: the Orbbec's JPEG
stream is served as MJPEG as is, the Go2's raw frames are JPEG-encoded here), stt/transcript, stt/request,
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
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import Bool, String

STATIC = Path(__file__).parent / "static"
CAMERA = os.environ.get("DASHBOARD_CAMERA") or "orbbec/color/image_raw/compressed"
RAW_FPS = 10.0  # raw camera: frames JPEG-encoded per second at most
RATE_WINDOW = 3.0  # s
VOICE_FILE = Path(os.environ.get("DASHBOARD_VOICE_FILE") or "/deictic/data/voice/roxy_voice.json")
PIPER_VOICES = os.environ.get("PIPER_VOICES_DIR") or "/opt/piper-voices"
MAX_TEXT = 400  # characters per typed or tested line
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

        if CAMERA.endswith("/compressed"):
            self.create_subscription(CompressedImage, CAMERA, self.on_camera, qos_profile_sensor_data)
        else:
            self.raw_t = 0.0
            self.create_subscription(Image, CAMERA, self.on_raw_camera, qos_profile_sensor_data)
        self.create_subscription(Odometry, "odom", lambda _: self.tick("odom"), 10)
        self.create_subscription(String, "stt/transcript", self.on_transcript, 10)
        self.create_subscription(String, "stt/request", self.on_request, 10)
        self.create_subscription(String, "voice/qa", self.on_qa, 10)
        self.create_subscription(Bool, "voice/speaking", self.on_speaking, LATCHED)
        self.create_subscription(String, "navila/step", self.on_step, 50)
        self.create_subscription(Log, "/rosout", self.on_log, ROSOUT)
        self.create_timer(1.0, self.push_status)
        threading.Thread(target=self.poll_server, daemon=True).start()

        # typing to Roxy and the voice lab
        self.typed_pub = self.create_publisher(String, "stt/typed", 10)
        self.instruction_pub = self.create_publisher(String, "navila/instruction", 10)
        self.speak_pub = self.create_publisher(String, "voice/speak", 10)
        self.voice_pub = self.create_publisher(String, "voice/config", LATCHED)
        self.typed = ("", 0.0)  # last typed request, to label it when wake_router echoes it on stt/request
        self.voice = None  # {"label", "spec"}: Roxy's voice as chosen here
        try:
            from voice import voice_fx, voice_library
            self.fx, self.library = voice_fx, voice_library
            self.voices = voice_fx.Voices(PIPER_VOICES, keep=2)
            self.render_lock = threading.Lock()
        except Exception as e:  # not the ROS image: the Voice tab says so
            self.fx = self.library = None
            self.get_logger().warn(f"voice lab off: {e}")
        if VOICE_FILE.is_file():
            try:
                self.use_voice(json.loads(VOICE_FILE.read_text()), save=False)
            except (ValueError, KeyError, TypeError) as e:
                self.get_logger().warn(f"ignoring {VOICE_FILE}: {e}")

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

    def on_raw_camera(self, msg):
        now = time.monotonic()
        if now - self.raw_t < 1.0 / RAW_FPS:
            self.tick("camera")
            return
        self.raw_t = now
        import cv2
        import numpy as np
        img = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.step)[:, :msg.width * 3]
        img = img.reshape(msg.height, msg.width, 3)
        if msg.encoding == "rgb8":
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            msg = CompressedImage(data=jpg.tobytes())
            self.on_camera(msg)

    def on_transcript(self, msg):
        if msg.data.strip():
            self.hub.publish("heard", {"t": time.time(), "text": msg.data.strip(), "wake": False})

    def on_request(self, msg):
        # wake_router: what followed "Hey Roxy" (empty = only the wake phrase), or a typed request
        text = msg.data.strip()
        typed = bool(text) and text == self.typed[0].strip(" ,.!?;:-") and time.time() - self.typed[1] < 5.0
        self.hub.publish("heard", {"t": time.time(), "text": text, "wake": True, "typed": typed})

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
                                    "server_url": self.server_url,
                                    "typing": self.count_subscribers("stt/typed") > 0,
                                    "tts": self.count_subscribers("voice/config") > 0,
                                    "voice": self.voice}, keep=False)

    # ---------------- typing and the voice lab (HTTP handlers call these) ----------------
    def type_request(self, text):
        if self.count_subscribers("stt/typed") == 0:
            raise RuntimeError("nothing is listening: is the bringup stack running?")
        self.typed = (text, time.time())
        self.typed_pub.publish(String(data=text))

    def stop(self):
        self.instruction_pub.publish(String(data="stop"))

    def speak(self, text, spec=None):
        """Say one line on Roxy's speaker: in `spec`, or Roxy's own voice for None."""
        if self.count_subscribers("voice/speak") == 0:
            raise RuntimeError("Roxy's speaker voice is off: start with VOICE=true and the Pi up")
        self.speak_pub.publish(String(data=json.dumps({"text": text, "voice": spec})))

    def use_voice(self, cfg, save=True):
        cfg = {"label": str(cfg.get("label") or "custom")[:80], "spec": self.fx.full(cfg["spec"])
               if self.fx else cfg["spec"]}
        self.voice = cfg
        self.voice_pub.publish(String(data=json.dumps(cfg)))
        if save:
            VOICE_FILE.parent.mkdir(parents=True, exist_ok=True)
            VOICE_FILE.write_text(json.dumps(cfg, indent=1))
        self.get_logger().info(f"Roxy's voice -> {cfg['label']}")

    def voice_catalog(self):
        if not self.fx:
            return {"ok": False, "error": "voice packages missing (run the dashboard in the ROS image)"}
        installed = set(self.voices.installed())
        voices = [{"name": k, **v, "installed": v["spec"]["model"] in installed}
                  for k, v in self.library.LIBRARY.items()]
        bases = [{"label": b[0], "model": b[1], "speaker": b[2], "installed": b[1] in installed}
                 for b in self.library.BASES]
        return {"ok": True, "voices": voices, "bases": bases, "defaults": self.fx.DEFAULTS,
                "current": self.voice, "installed": sorted(installed)}

    def render_preview(self, text, spec):
        with self.render_lock:  # one at a time: the laptop also runs speech recognition
            return self.fx.render(self.voices, text, spec)


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

        def send_json(self, obj, status=200):
            self.send_bytes(json.dumps(obj).encode(), "application/json", status)

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 100_000)) or b"{}")
                text = " ".join(str(body.get("text", "")).split())[:MAX_TEXT]
                if path == "/api/type":
                    if not text:
                        return self.send_json({"ok": False, "error": "empty"}, 400)
                    node.type_request(text)
                elif path == "/api/stop":
                    node.stop()
                elif path == "/api/speak":  # a line on Roxy's speaker: Roxy's voice, or a test voice
                    if not text:
                        return self.send_json({"ok": False, "error": "empty"}, 400)
                    node.speak(text, body.get("spec"))
                elif path == "/api/voice/use":
                    node.use_voice({"label": body.get("label"), "spec": body["spec"]})
                elif path == "/api/voice/preview":  # rendered here, played in the browser
                    if not node.fx:
                        return self.send_json({"ok": False, "error": "voice lab off"}, 503)
                    wav = node.render_preview(text or "Hello.", body.get("spec") or {})
                    return self.send_bytes(wav or b"", "audio/wav")
                else:
                    return self.send_json({"ok": False, "error": "not found"}, 404)
            except (ValueError, KeyError, TypeError) as e:
                return self.send_json({"ok": False, "error": f"bad request: {e}"}, 400)
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)[:300]}, 503)
            self.send_json({"ok": True})

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/voices":
                return self.send_json(node.voice_catalog())
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
