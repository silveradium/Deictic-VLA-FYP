"""Measure how the Go2 actually responds to velocity commands (`make calibrate`).

Turns in place at a range of commanded yaw rates, alternating left/right so the robot
ends where it started, and optionally walks forward/back at a range of speeds (--ros-args
-p forward:=true). For each command it measures from odometry:
  delay     time until the robot visibly starts moving
  rate      steady-state speed over the last second of the command
  coast     extra motion after the stop (StopMove sent)
The results set min_ang_speed_deg / min_lin_speed in configs/robots/roxy.yaml: the floor
must be a speed the robot really follows, or closed-loop steps stall short of the target.

Run with `make collect` (not infer: navila_node also publishes cmd_vel). Commands go to
cmd_vel at twist_mux priority 5, so the joystick overrides. Ctrl-C stops the robot.
"""

import math
import threading
import time

import rclpy
from geometry_msgs.msg import Twist
from go2_interfaces.msg import WebRtcReq
from nav_msgs.msg import Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions

TURN_RATES_DEG = [6, 9, 12, 15, 20, 25, 30, 45]  # deg/s
FORWARD_SPEEDS = [0.10, 0.15, 0.20, 0.30, 0.50]  # m/s
CMD_S, SETTLE_S, RATE_HZ = 2.5, 1.5, 20.0


def yaw_from_quat(q) -> float:
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class Calibrator(Node):
    def __init__(self):
        super().__init__("calibrate_motion")
        self.forward = self.declare_parameter("forward", False).value
        self.cmd_pub = self.create_publisher(Twist, "cmd_vel", 10)
        self.webrtc_pub = self.create_publisher(WebRtcReq, "webrtc_req", 10)
        self.samples = []  # (t, x, y, unwrapped yaw)
        self.lock = threading.Lock()
        self._yaw_prev, self._yaw_acc = None, 0.0
        self.create_subscription(Odometry, "odom", self.on_odom, 10)

    def on_odom(self, msg):
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        yaw = yaw_from_quat(q)
        if self._yaw_prev is not None:
            self._yaw_acc += math.atan2(math.sin(yaw - self._yaw_prev), math.cos(yaw - self._yaw_prev))
        self._yaw_prev = yaw
        with self.lock:
            self.samples.append((time.time(), p.x, p.y, self._yaw_acc))

    def window(self, t0, t1):
        with self.lock:
            return [s for s in self.samples if t0 <= s[0] <= t1]

    def send(self, vx=0.0, wz=0.0):
        msg = Twist()
        msg.linear.x, msg.angular.z = float(vx), float(wz)
        self.cmd_pub.publish(msg)

    def stop(self):
        self.send()
        self.webrtc_pub.publish(WebRtcReq(topic="rt/api/sport/request", api_id=1003, parameter="", priority=1))


def run_step(node, vx, wz):
    """Command (vx, wz) for CMD_S, stop, wait SETTLE_S. Returns (delay, rate, coast) in
    m or rad units: rate over the last second of the command, coast = motion after stop."""
    t0 = time.time()
    while time.time() - t0 < CMD_S:
        node.send(vx, wz)
        time.sleep(1.0 / RATE_HZ)
    t_stop = time.time()
    node.stop()
    time.sleep(SETTLE_S)

    def pos(s):  # progress along the commanded motion
        return s[3] if wz else math.hypot(s[1] - start[1], s[2] - start[2]) * (1 if vx > 0 else -1)

    run = node.window(t0, t_stop)
    after = node.window(t_stop, t_stop + SETTLE_S)
    if len(run) < 5 or not after:
        return None
    start = run[0]
    sign = math.copysign(1.0, wz or vx)
    moved = [(s[0] - t0, sign * (pos(s) - pos(start))) for s in run]
    thresh = math.radians(2.0) if wz else 0.02
    delay = next((t for t, d in moved if d > thresh), float("nan"))
    last = [m for m in moved if m[0] >= CMD_S - 1.0]
    rate = (last[-1][1] - last[0][1]) / max(last[-1][0] - last[0][0], 1e-3)
    coast = sign * (pos(after[-1]) - pos(run[-1]))
    return delay, rate, coast


def main():
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = Calibrator()
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    spinner = threading.Thread(target=ex.spin, daemon=True)
    spinner.start()
    log = node.get_logger()
    try:
        time.sleep(1.0)
        if any(i.node_name == "navila_node" for i in node.get_publishers_info_by_topic("cmd_vel")):
            log.error("navila_node is publishing cmd_vel: run this with `make collect`, not infer")
            return
        if not node.samples:
            log.error("no odometry on /odom: is the robot connected?")
            return
        log.warn("Roxy will TURN IN PLACE" + (" and WALK ~1 m forward/back" if node.forward else "")
                 + " in 5 s. Clear the area. Ctrl-C (or the joystick) stops it.")
        time.sleep(5.0)

        rows = []
        for i, deg in enumerate(TURN_RATES_DEG):
            sign = 1 if i % 2 == 0 else -1  # alternate left/right
            r = run_step(node, 0.0, sign * math.radians(deg))
            if r:
                d, rate, coast = r
                rows.append(f"  turn {deg:5.1f} deg/s -> {math.degrees(rate):5.1f} deg/s  "
                            f"delay {d:4.2f}s  coast {math.degrees(coast):5.1f} deg")
                log.info(rows[-1].strip())
        if node.forward:
            for i, v in enumerate(FORWARD_SPEEDS):
                sign = 1 if i % 2 == 0 else -1  # alternate forward/back
                r = run_step(node, sign * v, 0.0)
                if r:
                    d, rate, coast = r
                    rows.append(f"  walk {v:5.2f} m/s   -> {rate:5.2f} m/s    "
                                f"delay {d:4.2f}s  coast {coast * 100:5.1f} cm")
                    log.info(rows[-1].strip())
        print("\n=== Go2 response (commanded -> measured) ===\n" + "\n".join(rows), flush=True)
        print("Set min_ang_speed_deg / min_lin_speed in configs/robots/roxy.yaml to the lowest "
              "command the robot follows closely.", flush=True)
    except KeyboardInterrupt:
        log.warn("interrupted")
    finally:
        node.stop()
        time.sleep(0.3)  # let the stop go out
        ex.shutdown()
        spinner.join(timeout=2.0)
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
