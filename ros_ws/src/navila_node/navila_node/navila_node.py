"""NaVILA on the real Go2: camera frames + instruction -> NaVILA server -> cmd_vel.

ROS 2 port of navila_go2/real/navila_go2_node.py (NaVILA fork), with ROS parameters instead
of argparse so bringup can launch it. The client (frame history, HTTP) and the mid-level
controller (NaVILA text -> closed-loop velocity on odometry) are imported from the fork's
navila_go2.common, copied into the ROS image, so sim and robot share one implementation.

Instruction: publish std_msgs/String on navila/instruction (`make instruct TEXT="..."`).
"stop" or an empty string ends the episode and holds still.

Pointing: the gesture is made while the instruction is given, before the robot moves, so the frames
of the last gesture_window_s seconds are kept and gesture_frames of them start the history. The
training data is exported with the same rule and values (data_pipeline/real/navila_input.py).

Velocity goes to cmd_vel, twist_mux priority 5: the joystick (priority 10) always overrides.
Dry run unless enable_motion is true: commands are logged, not published.

Stopping: the go2 driver drops zero velocities, so the robot would only stop on its own
timeout. Whenever this node's command drops to zero (step done, stop, stale sensors,
shutdown) it also sends Unitree's StopMove through webrtc_req.

Each finished action is logged with what was asked vs. what odometry measured, to tune
the mid-level speeds (min_ang_speed_deg etc.) for the real Go2.
"""

import math
import time
from collections import deque

import cv2
import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from go2_interfaces.msg import WebRtcReq
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import Image
from std_msgs.msg import String

from navila_go2.common.client import FrameHistory, NaVilaClient
from navila_go2.common.mid_level import FORWARD, STOP, MidLevelConfig, MidLevelController

SPORT_TOPIC = "rt/api/sport/request"
STOP_MOVE = 1003  # Unitree sport API: StopMove


def image_to_rgb(msg: Image) -> np.ndarray:
    """sensor_msgs/Image (bgr8 / rgb8) -> HxWx3 uint8 RGB, without cv_bridge."""
    buf = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.step)[:, : msg.width * 3]
    img = buf.reshape(msg.height, msg.width, 3)
    return img[:, :, ::-1].copy() if msg.encoding.lower().startswith("bgr") else img.copy()


def to_navila_view(rgb: np.ndarray, mode: str, size: int = 512) -> np.ndarray:
    """NaVILA squares every frame. crop: center square (no distortion; preferred in the sim tests),
    squash: whole frame squeezed to a square."""
    if mode == "crop":
        h, w = rgb.shape[:2]
        s = min(h, w)
        rgb = rgb[(h - s) // 2 : (h + s) // 2, (w - s) // 2 : (w + s) // 2]
    return cv2.resize(rgb, (size, size), interpolation=cv2.INTER_AREA)


def yaw_from_quat(q) -> float:
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class NavilaNode(Node):
    def __init__(self):
        super().__init__("navila_node")
        p = self.declare_parameters("", [
            ("server_url", "http://127.0.0.1:8000"),
            ("camera_topic", "camera/image_raw"),
            ("odom_topic", "odom"),
            ("cmd_topic", "cmd_vel"),
            ("image_mode", "crop"),
            ("gesture_frames", 4),
            ("gesture_window_s", 3.0),
            ("instruction", ""),
            ("lin_speed", 0.5),
            ("ang_speed_deg", 30.0),
            # closed-loop slow-down floor near the target; the Go2 may not move below some speed
            ("min_lin_speed", 0.15),
            ("min_ang_speed_deg", 9.0),
            ("timeout_factor", 3.0),
            ("max_lin", 0.5),
            ("max_ang", 0.8),
            ("max_actions", 60),
            ("rate", 20.0),
            ("enable_motion", False),
        ])
        self.cfg = {param.name: param.value for param in p}

        self.client = NaVilaClient(self.cfg["server_url"])
        self.get_logger().info(f"waiting for NaVILA server {self.cfg['server_url']} ...")
        self.get_logger().info(f"NaVILA server: {self.client.wait_ready()}")
        self.ctrl = MidLevelController(MidLevelConfig(
            lin_speed=self.cfg["lin_speed"],
            ang_speed=math.radians(self.cfg["ang_speed_deg"]),
            min_lin_speed=self.cfg["min_lin_speed"],
            min_ang_speed=math.radians(self.cfg["min_ang_speed_deg"]),
            timeout_factor=self.cfg["timeout_factor"],
        ))
        self.history = FrameHistory()
        self.frame, self.frame_t = None, 0.0
        self.recent = deque()  # (t, frame) of the last gesture_window_s + 1 s, for the pointing frames
        self.pose, self.pose_t = None, 0.0
        self.instruction = self.cfg["instruction"] or None
        self.instruction_t = time.time()
        self.mode = "idle"
        self.future = None
        self.n_actions = 0
        self.units_recorded = 0
        self.moving = False  # last published command was non-zero
        self.action_t0 = 0.0

        self.cmd_pub = self.create_publisher(Twist, self.cfg["cmd_topic"], 10)
        self.decision_pub = self.create_publisher(String, "navila/decision", 10)
        self.webrtc_pub = self.create_publisher(WebRtcReq, "webrtc_req", 10)
        self.create_subscription(Image, self.cfg["camera_topic"], self.on_image, qos_profile_sensor_data)
        self.create_subscription(Odometry, self.cfg["odom_topic"], self.on_odom, 10)
        self.create_subscription(String, "navila/instruction", self.on_instruction, 10)
        self.create_timer(1.0 / self.cfg["rate"], self.tick)
        self.get_logger().info(
            f"mid level: {self.cfg['lin_speed']} m/s, {self.cfg['ang_speed_deg']} deg/s, "
            f"floor {self.cfg['min_lin_speed']} m/s / {self.cfg['min_ang_speed_deg']} deg/s")
        self.get_logger().info(f"camera: {self.cfg['camera_topic']} ({self.cfg['image_mode']}), "
                               f"waiting for an instruction on navila/instruction")
        if not self.cfg["enable_motion"]:
            self.get_logger().warn("DRY RUN: velocity commands are logged, not published (enable_motion:=true)")

    # ---------------- callbacks ----------------
    def on_image(self, msg):
        self.frame, self.frame_t = to_navila_view(image_to_rgb(msg), self.cfg["image_mode"]), time.time()
        self.recent.append((self.frame_t, self.frame))
        while self.recent[0][0] < self.frame_t - self.cfg["gesture_window_s"] - 1.0:
            self.recent.popleft()

    def on_odom(self, msg):
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        self.pose, self.pose_t = (p.x, p.y, yaw_from_quat(q)), time.time()

    def on_instruction(self, msg):
        text = msg.data.strip()
        self.ctrl.active = False
        self.future = None  # a reply still in flight is ignored
        if not text or text.lower() == "stop":
            self.instruction, self.mode = None, "done"
            self.get_logger().info("stopped by instruction")
        else:
            self.instruction, self.mode = text, "idle"  # restarts in tick()
            self.instruction_t = time.time()

    # ---------------- control loop ----------------
    def gesture_frames(self, t_end):
        """Frames nearest gesture_frames times spread over the gesture_window_s s before t_end, the last
        window / n before it, each within 0.5 s and used once. Same rule as
        data_pipeline/real/navila_input.py pick_gesture, which builds the training data: change both."""
        n, window = self.cfg["gesture_frames"], self.cfg["gesture_window_s"]
        before = [(t, f) for t, f in self.recent if t < t_end]
        picked = []
        for k in range(n):
            target = t_end - window * (n - k) / n
            t, frame = min(before, key=lambda tf: abs(tf[0] - target), default=(None, None))
            if t is not None and abs(t - target) <= 0.5 and not any(frame is p for p in picked):
                picked.append(frame)
        return picked

    def start_episode(self):
        self.history.reset()
        pointing = self.gesture_frames(self.instruction_t)
        for frame in pointing:
            self.history.add(frame)
        self.ctrl.reset_heading(self.pose[2])
        self.n_actions = 0
        self.get_logger().info(f"new instruction: {self.instruction!r} "
                               f"({len(pointing)} of {self.cfg['gesture_frames']} pointing frames)")
        self.query()

    def query(self):
        self.history.add(self.frame)
        self.future = self.client.act_async(self.instruction, self.history)
        self.mode = "thinking"

    def publish(self, vx, vy, wz):
        vx = max(-self.cfg["max_lin"], min(self.cfg["max_lin"], vx))
        wz = max(-self.cfg["max_ang"], min(self.cfg["max_ang"], wz))
        msg = Twist()
        msg.linear.x, msg.linear.y, msg.angular.z = float(vx), float(vy), float(wz)
        moving = abs(vx) + abs(vy) + abs(wz) > 0
        if self.cfg["enable_motion"]:
            self.cmd_pub.publish(msg)
            if self.moving and not moving:
                self.stop_move()
        elif moving:
            self.get_logger().info(f"[dry run] cmd vx={vx:.2f} wz={wz:.2f}", throttle_duration_sec=1.0)
        self.moving = moving

    def stop_move(self):
        """Explicit stop: zero velocities never reach the robot (the driver drops them)."""
        self.webrtc_pub.publish(WebRtcReq(topic=SPORT_TOPIC, api_id=STOP_MOVE, parameter="", priority=1))

    def log_outcome(self):
        a, c = self.ctrl.action, self.ctrl
        asked, done, unit = (a.value * 100, c.progress * 100, "cm") if a.kind == FORWARD else (a.value, c.progress, "deg")
        self.get_logger().info(
            f"   done {a}: measured {done:.0f} {unit} of {asked:.0f} in {time.time() - self.action_t0:.1f}s"
            + ("  TIMED OUT" if c.timed_out else ""))

    def tick(self):
        now = time.time()
        stale = self.frame is None or self.pose is None or now - self.frame_t > 0.5 or now - self.pose_t > 0.5
        if stale:
            if self.mode in ("thinking", "executing"):
                self.get_logger().warn("camera/odom stale: holding still", throttle_duration_sec=2.0)
            elif self.instruction:
                self.get_logger().warn("waiting for camera and odom before starting", throttle_duration_sec=5.0)
            self.publish(0.0, 0.0, 0.0)
            return
        if self.mode == "idle" and self.instruction:
            self.start_episode()

        if self.mode == "thinking" and self.future is not None and self.future.done():
            fut, self.future = self.future, None
            try:
                action, out = fut.result()
            except Exception as e:  # never keep moving on a failed decision
                self.get_logger().error(f"NaVILA request failed: {e}; stopping")
                self.mode, self.instruction = "done", None
                action = None
            if action is not None:
                self.n_actions += 1
                self.get_logger().info(f"#{self.n_actions:02d} {out['text']!r} -> {action} ({out['latency_s']:.2f}s)")
                self.decision_pub.publish(String(data=out["text"]))
                if action.kind == STOP or self.n_actions >= self.cfg["max_actions"]:
                    self.mode, self.instruction = "done", None
                    self.get_logger().info("episode finished")
                else:
                    self.ctrl.start(action, self.pose)
                    self.units_recorded = 0
                    self.action_t0 = time.time()
                    self.mode = "executing"

        cmd = (0.0, 0.0, 0.0)
        if self.mode == "executing":
            cmd = self.ctrl.step(self.pose, 1.0 / self.cfg["rate"])
            while self.units_recorded < self.ctrl.units_done and self.ctrl.active:
                self.history.add(self.frame)  # one frame per 25 cm / 15 deg, as in NaVILA's training data
                self.units_recorded += 1
            if not self.ctrl.active:
                self.log_outcome()
                self.query()
        # publish every tick (zeros too): twist_mux drops the input after 0.5 s of silence
        self.publish(*cmd)


def main():
    # Python's own SIGINT handling (KeyboardInterrupt) instead of rclpy's, which would shut
    # the context down before the final StopMove could be published
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = NavilaNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.publish(0.0, 0.0, 0.0)  # sends StopMove if the robot was moving
        except Exception:
            pass  # context already shut down by the signal
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
