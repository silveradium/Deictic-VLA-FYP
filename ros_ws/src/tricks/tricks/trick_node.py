"""Voice tricks: voice/trick (a name from tricks.catalog) -> Unitree sport commands on webrtc_req.

  "Roxy, sit down"  -> dialog_node (Gemini) picks "sit" -> Sit
  "Roxy, dance!"    -> "dance" -> stands up first if Roxy is sitting or lying, then Dance1
  "Hi Roxy"         -> "hello" -> a wave
  by hand, while a stack runs: `make trick NAME=sit`

Posture is tracked from the commands sent here, so a trick that needs Roxy standing stands her up
first. Moving her with the remote in between can fool it; "stand up" always works.

A running NaVILA episode is stopped before a trick (navila/instruction "stop"). come_forward is an
exact move for navila_node ("forward 1 m", closed loop on odometry): infer / demo-site stacks
only, and it moves only with MOTION=1 like every NaVILA command. Other tricks go straight to the
robot, dry run or not. "Stop" (wake_router) ends a trick at once with StopMove.

Parameters: configs/robots/roxy.yaml, trick_node.
"""

import json
import time

import rclpy
from go2_interfaces.msg import WebRtcReq
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String

from tricks.catalog import BALANCE_STAND, RISE_SIT, SPORT_TOPIC, STAND_UP, STOP_MOVE, TRICKS


class TrickNode(Node):
    def __init__(self):
        super().__init__("trick_node")
        p = self.declare_parameter
        self.stand_settle = p("stand_settle", 3.0).value  # s for StandUp / RiseSit before the next command
        self.forward_instruction = p("forward_instruction", "forward 1 m").value  # come_forward

        self.posture = "standing"  # assumed at start: what the remote leaves the Go2 in
        self.queue = []  # [(kind, arg, wait_s)], run one at a time by tick()
        self.next_t = 0.0
        self.navila_active = False
        self.own_stops = 0  # our own "stop"s to NaVILA, which must not cancel the trick they precede

        self.webrtc_pub = self.create_publisher(WebRtcReq, "webrtc_req", 10)
        self.instruction_pub = self.create_publisher(String, "navila/instruction", 10)
        self.create_subscription(String, "voice/trick", self.on_trick, 10)
        self.create_subscription(String, "navila/instruction", self.on_instruction, 10)
        self.create_subscription(String, "navila/step", self.on_step, 10)
        self.create_timer(0.1, self.tick)
        self.get_logger().info(f"tricks ready: {', '.join(TRICKS)}")

    # ---------------- inputs ----------------
    def on_trick(self, msg):
        name = msg.data.strip().lower()
        trick = TRICKS.get(name)
        if trick is None:
            self.get_logger().warn(f"unknown trick {name!r}: one of {', '.join(TRICKS)}")
            return
        steps = []
        if self.navila_active and name != "come_forward":  # come_forward replaces the episode itself
            steps.append(("instruction", "stop", 0.5))
        if name == "stand_up" or self.posture != "standing":
            steps += self.stand_steps()
        if name == "come_forward":
            steps.append(("instruction", self.forward_instruction, 0.0))
        elif trick.api_id is not None:
            steps.append(("api", trick.api_id, 0.0))
        self.posture = trick.posture
        self.queue, self.next_t = steps, 0.0  # replaces anything still queued
        self.get_logger().info(f"trick {name}: " + ", ".join(f"{k} {a}" for k, a, _ in steps))

    def stand_steps(self):
        if self.posture == "sitting":
            return [("api", RISE_SIT, self.stand_settle)]
        # StandUp locks the joints; BalanceStand hands back to the normal (walkable) stand
        return [("api", STAND_UP, self.stand_settle), ("api", BALANCE_STAND, 1.0)]

    def on_instruction(self, msg):
        if msg.data.strip().lower() != "stop":
            return
        if self.own_stops:
            self.own_stops -= 1
        else:  # also ends a trick the robot is still playing (a dance runs ~20 s)
            self.queue = []
            self.send(STOP_MOVE)

    def on_step(self, msg):
        try:
            event = json.loads(msg.data).get("type")
        except ValueError:
            return
        if event == "instruction":
            self.navila_active = True
        elif event in ("finished", "stopped"):
            self.navila_active = False

    # ---------------- output ----------------
    def tick(self):
        if not self.queue or time.monotonic() < self.next_t:
            return
        kind, arg, wait = self.queue.pop(0)
        if kind == "api":
            self.send(arg)
        else:
            self.own_stops += arg == "stop"
            self.instruction_pub.publish(String(data=arg))
        self.next_t = time.monotonic() + wait

    def send(self, api_id):
        self.webrtc_pub.publish(WebRtcReq(topic=SPORT_TOPIC, api_id=api_id, parameter="", priority=1))


def main():
    rclpy.init()
    node = TrickNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
