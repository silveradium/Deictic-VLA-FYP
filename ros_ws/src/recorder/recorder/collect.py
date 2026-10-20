"""Terminal data collection: one bag per episode, driven from the keyboard.

  make collect     (starts bringup if it isn't running, then this, in the foreground)

  Enter    new episode: recording starts. Point at the target and type the instruction
           ("go to that door")
  Enter    instruction done: drive Roxy to the target with the remote
  Ctrl-C   end the episode, then Enter = keep, d = discard, e = edit the instruction
  q        quit (Ctrl-C at the prompt does the same)

Recording starts at the first Enter, not the second, so the frames of the pointing gesture are in
the bag. The instruction is typed by hand (no mic); left empty, it is typed after the episode.

Only raw data is saved: the instruction, every topic in recorder.topics, and the times. No action
labels are computed here or by `make preview` -- the label format is still open, and odometry in the
bag is enough to derive them later (data_pipeline/real/path_stats.py summarises the driving).

Every topic in roxy.yaml recorder.topics is subscribed for the whole session (raw bytes, never
decoded, with the publisher's QoS), so recording starts instantly; messages are only written while
an episode runs. Latched topics (tf_static) are written again at the start of every episode.

Layout under data_dir:
  sessions/session_YYYYmmdd_HHMMSS/
    configs/            copy of configs/ when the session started
    ep_001/             rosbag2 (mcap) + episode.json: instruction, times, counts
    discarded/ep_002/   discarded or cancelled episodes, kept just in case
"""

import json
import math
import os
import shutil
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

import rclpy
import rosbag2_py
from nav_msgs.msg import Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message, serialize_message
from rclpy.signals import SignalHandlerOptions
from rosidl_runtime_py.utilities import get_message
from std_msgs.msg import String

CAMERA = "/orbbec/color/image_raw/compressed"
ODOM = "/odom"
EVENT = "/episode/event"
RATE_WINDOW = 3.0  # s, for the live rates


def yaw_deg(q) -> float:
    return math.degrees(math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)))


def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


class Collector(Node):
    """Subscribes to everything to record and writes it into the open episode bag, if any."""

    def __init__(self):
        super().__init__("recorder")  # picks up the recorder block of roxy.yaml
        topics = self.declare_parameter("topics", [CAMERA, ODOM]).value
        self.data_dir = self.declare_parameter("data_dir", "/deictic/data").value
        self.config_dir = self.declare_parameter("config_dir", "/deictic/configs").value
        # camera and odom are needed for the live status anyway
        self.wanted = {t if t.startswith("/") else "/" + t for t in topics} | {CAMERA, ODOM}
        self.wanted.discard(EVENT)  # written directly, never subscribed

        self.lock = threading.Lock()  # guards the writer: callbacks write, the terminal opens / closes
        self.writer = None
        self.bag_topics = set()
        self.counts = defaultdict(int)  # messages written into the open bag, per topic
        self.types = {EVENT: "std_msgs/msg/String"}
        self.latched = {}  # last message of each latched topic
        self.subs, self.skipped = {}, set()
        self.recent = defaultdict(deque)  # receive times of the last RATE_WINDOW s, per topic
        self.pose = None  # (x, y, yaw_deg)
        self.start_pose, self.path_m = None, 0.0  # of the open episode

        self.event_pub = self.create_publisher(String, "episode/event", 10)
        self.create_timer(1.0, self.discover)
        self.discover()

    # ---------------- subscriptions ----------------
    def discover(self):
        """Subscribe to wanted topics as their publishers appear, with matching QoS."""
        for name, types in self.get_topic_names_and_types():
            if name not in self.wanted or name in self.subs or name in self.skipped:
                continue
            pubs = self.get_publishers_info_by_topic(name)
            if not pubs:
                continue
            try:
                msg_type = get_message(types[0])
            except (AttributeError, ModuleNotFoundError, ValueError):
                self.get_logger().warn(f"not recording {name}: message type {types[0]} unknown here")
                self.skipped.add(name)
                continue
            reliable = all(p.qos_profile.reliability == ReliabilityPolicy.RELIABLE for p in pubs)
            latched = all(p.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for p in pubs)
            qos = QoSProfile(
                history=HistoryPolicy.KEEP_LAST, depth=100 if reliable else 20,
                reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)
            self.types[name] = types[0]
            self.subs[name] = self.create_subscription(
                msg_type, name, lambda data, n=name, l=latched: self.on_msg(n, data, l), qos, raw=True)

    def on_msg(self, topic, data, latched):
        now = time.time()
        self.recent[topic].append(now)
        if latched:
            self.latched[topic] = data
        if topic == ODOM:
            m = deserialize_message(data, Odometry)
            p = m.pose.pose.position
            pose = (p.x, p.y, yaw_deg(m.pose.pose.orientation))
            if self.writer is not None:
                if self.start_pose is None:
                    self.start_pose = pose
                elif self.pose is not None:
                    self.path_m += math.hypot(pose[0] - self.pose[0], pose[1] - self.pose[1])
            self.pose = pose
        with self.lock:
            if self.writer is not None:
                self._write(topic, data, now)

    def rate(self, topic) -> float:
        q, now = self.recent[topic], time.time()
        while q and q[0] < now - RATE_WINDOW:
            q.popleft()
        return len(q) / RATE_WINDOW

    # ---------------- bags ----------------
    def _write(self, topic, data, t):  # with self.lock held
        if topic not in self.bag_topics:
            self.writer.create_topic(rosbag2_py.TopicMetadata(
                name=topic, type=self.types[topic], serialization_format="cdr", offered_qos_profiles=""))
            self.bag_topics.add(topic)
        self.writer.write(topic, data, int(t * 1e9))
        self.counts[topic] += 1

    def open_bag(self, path):
        writer = rosbag2_py.SequentialWriter()
        writer.open(rosbag2_py.StorageOptions(uri=path, storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
        with self.lock:
            self.writer, self.bag_topics, self.counts = writer, set(), defaultdict(int)
            self.start_pose, self.path_m = self.pose, 0.0
            now = time.time()
            for topic, data in self.latched.items():
                self._write(topic, data, now)

    def close_bag(self):
        with self.lock:
            writer, self.writer = self.writer, None
            counts = dict(self.counts)
        writer.close()
        motion = {"path_m": round(self.path_m, 3)}
        if self.start_pose is not None and self.pose is not None:
            sx, sy, syaw = self.start_pose
            motion["displacement_m"] = round(math.hypot(self.pose[0] - sx, self.pose[1] - sy), 3)
            motion["turned_deg"] = round(wrap_deg(self.pose[2] - syaw), 1)
        return counts, motion

    def turned(self):
        if self.start_pose is None or self.pose is None:
            return float("nan")
        return wrap_deg(self.pose[2] - self.start_pose[2])

    def event(self, **fields):
        """Publish an episode event (for Foxglove) and write it into the open bag."""
        msg = String(data=json.dumps({"t": round(time.time(), 3), **fields}))
        self.event_pub.publish(msg)
        with self.lock:
            if self.writer is not None:
                self._write(EVENT, serialize_message(msg), time.time())


class Terminal:
    def __init__(self, node, session, session_dir):
        self.node, self.session, self.session_dir = node, session, session_dir
        self.n = 0
        self.kept, self.discarded = [], []

    def status(self):
        n = self.node
        odom, cam = n.rate(ODOM), n.rate(CAMERA)
        line = f"odom {odom:.0f} Hz | camera {cam:.0f} fps"
        problems = [msg for msg, ok in (
            ("NO ODOM: is the Go2 connected? (make logs)", odom > 0),
            ("NO CAMERA: is the Pi camera up? (make pi-up on the Pi)", cam > 0)) if not ok]
        return line, problems

    def wait_ready(self, timeout=40.0):
        print("waiting for the Go2 odometry and the Orbbec camera ...", flush=True)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.node.rate(ODOM) > 0 and self.node.rate(CAMERA) > 0:
                return
            time.sleep(0.5)

    def run(self):
        print(f"\nSession {self.session}\n  -> {self.session_dir}")
        self.wait_ready()
        while True:
            line, problems = self.status()
            print(f"\n[{line}]  kept {len(self.kept)}, discarded {len(self.discarded)}")
            for p in problems:
                print(f"  ! {p}")
            try:
                cmd = input(f"Episode {self.n + 1}: [Enter] start   [q] quit > ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if cmd in ("q", "quit", "exit"):
                break
            self.episode()
        print(f"\nSession {self.session}: {len(self.kept)} episodes kept, {len(self.discarded)} discarded")
        print(f"  {self.session_dir}")
        print("  make preview    to look inside the bags")
        print("  bringup is still running (the Go2 stays connected): make down to stop it")

    def episode(self):
        node = self.node
        self.n += 1
        name = f"ep_{self.n:03d}"
        path = os.path.join(self.session_dir, name)
        node.open_bag(path)
        t_listen = time.time()
        node.event(event="listen", episode=self.n)
        print(f"\n● REC {name}  point at the target, type the instruction, then Enter")
        try:
            instruction = input("  instruction > ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            node.event(event="end", episode=self.n, reason="cancelled")
            node.close_bag()
            self.discard(name, path, None)
            print("  cancelled (in discarded/)")
            return
        t_go = time.time()
        node.event(event="go", episode=self.n, instruction=instruction)
        print(f'  instruction: "{instruction}"' if instruction
              else "  (no instruction: type it after the episode)")
        print("  DRIVE Roxy to the target with the remote, Ctrl-C when it has stopped")
        try:
            while True:
                time.sleep(0.5)
                turned = node.turned()
                motion = "no odom" if math.isnan(turned) else f"{node.path_m:.2f} m driven, turned {turned:+.0f} deg"
                print(f"\r  {time.time() - t_listen:5.1f} s | {node.counts.get(CAMERA, 0)} frames | "
                      f"odom {node.rate(ODOM):.0f} Hz | {motion}   ", end="", flush=True)
        except KeyboardInterrupt:
            print()
        t_end = time.time()
        node.event(event="end", episode=self.n, reason="ctrl-c")
        counts, motion = node.close_bag()

        meta = {"session": self.session, "episode": self.n, "instruction": instruction,
                "t_listen": round(t_listen, 3), "t_go": round(t_go, 3), "t_end": round(t_end, 3),
                "listen_s": round(t_go - t_listen, 2), "drive_s": round(t_end - t_go, 2),
                "frames": counts.get(CAMERA, 0), "odom_msgs": counts.get(ODOM, 0),
                **motion, "topics": counts}
        if not meta["frames"]:
            print("  ! no camera frames in this episode")
        if not meta["odom_msgs"]:
            print("  ! no odometry in this episode: no action labels")
        self.review(name, path, meta)

    def review(self, name, path, meta):
        while True:
            if not meta["instruction"]:
                print("  no instruction yet: e to type it, d to discard")
            try:
                ans = input("  [Enter] keep   [d] discard   [e] edit instruction > ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                ans = "" if meta["instruction"] else "d"
            if ans == "d":
                self.discard(name, path, meta)
                print(f"  {name} discarded (in discarded/)")
                return
            if ans == "e":
                try:
                    text = input("  instruction > ").strip()
                except (KeyboardInterrupt, EOFError):
                    text = ""
                if text:
                    meta["instruction"] = text
                    print(f'  instruction: "{text}"')
                continue
            if ans == "" and meta["instruction"]:
                meta["kept"] = True
                self.save_meta(path, meta)
                self.kept.append(name)
                print(f"  {name} saved: {meta['listen_s'] + meta['drive_s']:.1f} s, {meta['frames']} frames, "
                      f"{meta.get('path_m', 0):.2f} m")
                return

    def discard(self, name, path, meta):
        if meta is not None:
            meta["kept"] = False
            self.save_meta(path, meta)
        dest = os.path.join(self.session_dir, "discarded")
        os.makedirs(dest, exist_ok=True)
        shutil.move(path, os.path.join(dest, name))
        self.discarded.append(name)

    @staticmethod
    def save_meta(path, meta):
        with open(os.path.join(path, "episode.json"), "w") as f:
            json.dump(meta, f, indent=2)


def main():
    # Python's own SIGINT handling: Ctrl-C ends an episode instead of shutting ROS down
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = Collector()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    session = datetime.now().strftime("session_%Y%m%d_%H%M%S")
    session_dir = os.path.join(node.data_dir, "sessions", session)
    os.makedirs(session_dir)
    if os.path.isdir(node.config_dir):
        shutil.copytree(node.config_dir, os.path.join(session_dir, "configs"))
    try:
        Terminal(node, session, session_dir).run()
    finally:
        if node.writer is not None:
            node.close_bag()
        executor.shutdown(timeout_sec=1.0)
        spin_thread.join(timeout=2.0)  # stop spinning before the context goes, or rclpy aborts
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
