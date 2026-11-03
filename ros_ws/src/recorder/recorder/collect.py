"""Terminal data collection: one bag per episode, driven from the keyboard.

  make collect     (starts bringup if it isn't running, then this, in the foreground)

  session name  asked once, at the very start: S1-R1. A new name makes a new session folder, a
                name that already exists adds episodes to that folder
  Enter         new episode: recording starts. Point at the target, then drive Roxy to it
  Enter         Roxy has stopped: recording ends
  instruction   typed after the recording ("go to that door"), it does not matter when
  episode name  typed next: a new name is a new episode, an existing one is overwritten
  q             quit (Ctrl-C at the prompt does the same)

The whole episode is recorded, pointing gesture included, from the first Enter to the second. The
instruction and the name are typed afterwards, so neither eats into the recording. Nothing is
discarded here: to drop an episode, delete its folder, or record it again under the same name.

Only raw data is saved: the instruction, every topic in recorder.topics, and the times. No action
labels are computed here or by `make preview` -- the label format is still open, and odometry in the
bag is enough to derive them later (data_pipeline/real/path_stats.py summarises the driving).

Every topic in roxy.yaml recorder.topics is subscribed for the whole session (raw bytes, never
decoded, with the publisher's QoS), so recording starts instantly; messages are only written while
an episode runs. Latched topics (tf_static) are written again at the start of every episode.

Layout under data_dir:
  sessions/S1-R1/
    configs/            copy of configs/ when the session started
    ep_001/             rosbag2 (mcap) + episode.json: instruction, times, counts
    door-again/         any name typed at the prompt
"""

import json
import math
import os
import re
import shutil
import threading
import time
from collections import defaultdict, deque

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
TMP = "_recording"  # the bag folder while an episode runs, before it has a name
RESERVED = {"configs", "preview", "discarded", TMP}  # the session folder's own, never episodes


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
        self.recorded = []

    # ---------------- episode folders ----------------
    def episodes(self):
        """The episode folders already in the session folder."""
        return sorted(d for d in os.listdir(self.session_dir)
                      if d not in RESERVED and not d.startswith(".")
                      and os.path.isdir(os.path.join(self.session_dir, d)))

    def next_name(self):
        """ep_NNN after the highest ep_NNN already there, the default episode name."""
        used = [int(m.group(1)) for m in (re.fullmatch(r"ep_(\d+)", d) for d in self.episodes()) if m]
        return f"ep_{max(used, default=0) + 1:03d}"

    # ---------------- status ----------------
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
        here = self.episodes()
        if here:
            print(f"  {len(here)} episode(s) already in it: {', '.join(here)}")
        self.wait_ready()
        while True:
            line, problems = self.status()
            print(f"\n[{line}]  {len(self.recorded)} episode(s) recorded this run")
            for p in problems:
                print(f"  ! {p}")
            try:
                cmd = input(f"Next episode ({self.next_name()}): [Enter] start   [q] quit > ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if cmd in ("q", "quit", "exit"):
                break
            self.episode()
        print(f"\nSession {self.session}: {len(self.recorded)} episode(s) this run, "
              f"{len(self.episodes())} in the folder")
        print(f"  {self.session_dir}")
        print("  make preview    to look inside the bags")
        print("  bringup is still running (the Go2 stays connected): make down to stop it")

    # ---------------- one episode ----------------
    def episode(self):
        node = self.node
        tmp = os.path.join(self.session_dir, TMP)
        if os.path.isdir(tmp):  # left behind by a crash
            shutil.rmtree(tmp)
        node.open_bag(tmp)
        t_start = time.time()
        node.event(event="start", session=self.session)
        print("\n● REC  point at the target, then drive Roxy to it with the remote:")
        print("       turn and walk forward, no strafing, no reversing")
        print("       Enter when it has stopped")
        self.wait_enter(t_start)
        t_end = time.time()
        node.event(event="end", session=self.session)
        counts, motion = node.close_bag()
        print(f"  ■ {t_end - t_start:.1f} s, {counts.get(CAMERA, 0)} frames, "
              f"{motion.get('path_m', 0):.2f} m driven")
        if not counts.get(CAMERA, 0):
            print("  ! no camera frames in this episode")
        if not counts.get(ODOM, 0):
            print("  ! no odometry in this episode: no action labels")

        instruction = self.ask_instruction()
        name = self.ask_name()
        meta = {"session": self.session, "episode": name, "instruction": instruction,
                "t_start": round(t_start, 3), "t_end": round(t_end, 3),
                "duration_s": round(t_end - t_start, 2),
                "frames": counts.get(CAMERA, 0), "odom_msgs": counts.get(ODOM, 0),
                **motion, "topics": counts}
        path = self.store(tmp, name)
        self.save_meta(path, meta)
        self.recorded.append(name)
        print(f"  {name} saved: {meta['duration_s']:.1f} s, {meta['frames']} frames, "
              f"{meta.get('path_m', 0):.2f} m")

    def wait_enter(self, t_start):
        """Show the live status until Enter (or Ctrl-C) ends the episode."""
        stop = threading.Event()
        ticker = threading.Thread(target=self.ticker, args=(t_start, stop), daemon=True)
        ticker.start()
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            stop.set()
            ticker.join(timeout=1.0)
        print()

    def ticker(self, t_start, stop):
        node = self.node
        while not stop.wait(0.5):
            turned = node.turned()
            motion = "no odom" if math.isnan(turned) else f"{node.path_m:.2f} m driven, turned {turned:+.0f} deg"
            print(f"\r  {time.time() - t_start:5.1f} s | {node.counts.get(CAMERA, 0)} frames | "
                  f"odom {node.rate(ODOM):.0f} Hz | {motion}   ", end="", flush=True)

    def ask_instruction(self):
        """What was pointed at, typed after the recording. Empty is allowed, but asked twice."""
        for first in (True, False):
            try:
                text = input("  instruction > ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return ""
            if text or not first:
                return text
            print("  ! the episode is unusable without one; Enter again to leave it empty")
        return ""

    def ask_name(self):
        """The folder the episode is saved in. An existing name overwrites that episode."""
        default = self.next_name()
        while True:
            try:
                typed = input(f"  episode name [{default}] > ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return default
            name = clean_name(typed) if typed else default
            if not name:
                print("  ! letters, digits, - and _ please")
                continue
            if name in RESERVED:
                print(f"  ! {name} is the session folder's own, pick another name")
                continue
            if typed and name != typed:
                print(f"  using {name}")
            if os.path.isdir(os.path.join(self.session_dir, name)):
                print(f"  {name} already exists: overwriting it")
            return name

    def store(self, tmp, name):
        """Move the closed bag into its episode folder, renaming the bag files with it."""
        dest = os.path.join(self.session_dir, name)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.move(tmp, dest)
        for f in os.listdir(dest):
            if f.startswith(TMP):
                os.rename(os.path.join(dest, f), os.path.join(dest, name + f[len(TMP):]))
        metadata = os.path.join(dest, "metadata.yaml")
        if os.path.isfile(metadata):  # it holds the bag files' names
            with open(metadata) as f:
                text = f.read()
            with open(metadata, "w") as f:
                f.write(text.replace(TMP, name))
        return dest

    @staticmethod
    def save_meta(path, meta):
        with open(os.path.join(path, "episode.json"), "w") as f:
            json.dump(meta, f, indent=2)


def clean_name(text) -> str:
    """A folder name from typed text: "S1 R1" -> "S1-R1", nothing hidden or nested."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-.")


def ask_session(sessions_dir) -> str | None:
    """The session, asked once at the start. An existing name adds episodes to that folder."""
    while True:
        try:
            typed = input("Session name (e.g. S1-R1) > ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return None
        name = clean_name(typed)
        if not name:
            print("  ! letters, digits, - and _ please")
            continue
        if name != typed:
            print(f"  using {name}")
        print(f"  {'session exists: adding episodes to it' if os.path.isdir(os.path.join(sessions_dir, name)) else 'new session'}")
        return name


def main():
    # Python's own SIGINT handling: Ctrl-C at a prompt quits instead of shutting ROS down
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = Collector()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    try:
        sessions_dir = os.path.join(node.data_dir, "sessions")
        session = ask_session(sessions_dir)
        if session is not None:
            session_dir = os.path.join(sessions_dir, session)
            os.makedirs(session_dir, exist_ok=True)
            if os.path.isdir(node.config_dir):
                shutil.copytree(node.config_dir, os.path.join(session_dir, "configs"), dirs_exist_ok=True)
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
