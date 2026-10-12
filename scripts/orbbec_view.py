"""Live view of the Pi's Orbbec colour stream in a browser, with the field of view it covers.

  make orbbec-view                      then open http://localhost:8090
  make orbbec-view PORT=9000

Serves the JPEG stream (orbbec/color/image_raw/compressed) as MJPEG without re-encoding, so it
only needs rclpy. Horizontal / vertical FOV come from orbbec/color/camera_info:
2 * atan(size / (2 * focal)).
"""

import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile
from sensor_msgs.msg import CameraInfo, CompressedImage

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Orbbec view</title>
<style>body{margin:0;background:#111;color:#ddd;font:14px monospace}
#info{padding:8px}img{display:block;max-width:100vw;max-height:calc(100vh - 40px);margin:auto}</style>
</head><body><div id="info">waiting for /orbbec/color/image_raw/compressed ...</div>
<img src="/stream"><script>
setInterval(async () => {
  try {
    const s = await (await fetch("/info")).json();
    document.getElementById("info").textContent = s.frames === 0
      ? "waiting for /orbbec/color/image_raw/compressed ... (make pi-up on the Pi?)"
      : `${s.width}x${s.height}  ${s.fps.toFixed(1)} fps  ` +
        (s.hfov ? `HFOV ${s.hfov.toFixed(1)}°  VFOV ${s.vfov.toFixed(1)}°` : "no camera_info yet") +
        (s.age > 1 ? `  STALE ${s.age.toFixed(1)} s` : "");
  } catch (e) {}
}, 1000);
</script></body></html>"""


class Feed(Node):
    def __init__(self):
        super().__init__("orbbec_view")
        qos = QoSProfile(depth=1)  # reliable, as published (configs/topics.yaml); depth 1: latest frame only
        self.create_subscription(CompressedImage, "orbbec/color/image_raw/compressed", self.on_image, qos)
        self.create_subscription(CameraInfo, "orbbec/color/camera_info", self.on_info, qos)
        self.cond = threading.Condition()
        self.jpeg = None
        self.frames = 0
        self.stamps = []  # receive times of the last frames, for the fps
        self.info = {}

    def on_image(self, msg):
        now = time.monotonic()
        with self.cond:
            self.jpeg = bytes(msg.data)
            self.frames += 1
            self.stamps = [t for t in self.stamps if now - t < 2.0] + [now]
            self.cond.notify_all()

    def on_info(self, msg):
        fx, fy = msg.k[0], msg.k[4]
        if fx > 0 and fy > 0:
            self.info = {
                "width": msg.width, "height": msg.height,
                "hfov": math.degrees(2 * math.atan(msg.width / (2 * fx))),
                "vfov": math.degrees(2 * math.atan(msg.height / (2 * fy))),
            }

    def status(self):
        with self.cond:
            s = self.stamps
            fps = (len(s) - 1) / (s[-1] - s[0]) if len(s) > 1 and s[-1] > s[0] else 0.0
            age = time.monotonic() - s[-1] if s else 0.0
            return {"frames": self.frames, "fps": fps, "age": age, "width": 0, "height": 0, **self.info}


def make_handler(feed):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self.send(PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/info":
                self.send(json.dumps(feed.status()).encode(), "application/json")
            elif self.path == "/stream":
                self.stream()
            else:
                self.send_error(404)

        def stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                while True:
                    with feed.cond:
                        feed.cond.wait_for(lambda: feed.frames != seen, timeout=5.0)
                        if feed.frames == seen or feed.jpeg is None:
                            continue
                        seen, jpeg = feed.frames, feed.jpeg
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\n\r\n" % len(jpeg) + jpeg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser tab closed

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    rclpy.init()
    feed = Feed()
    threading.Thread(target=rclpy.spin, args=(feed,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(feed))
    server.daemon_threads = True
    feed.get_logger().info(f"Orbbec view on http://localhost:{args.port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        feed.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
