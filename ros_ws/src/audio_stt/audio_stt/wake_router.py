"""Wake phrase -> NaVILA: stt/transcript -> stt/request and navila/instruction.

  "Hey Roxy, walk to the door and stop"  -> navila/instruction "walk to the door and stop"
  "Hey Roxy" ... "go over there"         -> the next sentence (within followup_window s) is the request
  "Stop!" (wake phrase or not)           -> navila/instruction "stop" at once
  "Hi Roxy" / "Hello Roxy" alone        -> stt/request "hi" (with voice: Gemini waves back), and
                                           the next sentence is still the request
  anything else                          -> ignored

Requests are forwarded word for word: deictic words ("there", "that one") must reach NaVILA as
spoken, because the model pairs them with the pointing gesture. stt/request carries the same text
(empty for the wake phrase alone) for anything that wants to react to it; with
forward_to_navila:=false only that is published and another node decides what reaches NaVILA.
Parameters: configs/robots/roxy.yaml, wake_router.
"""

import re
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)

# Words that alone don't make a request ("Hey Roxy, um." is the wake phrase alone). Not "there":
# with a pointing gesture, "Hey Roxy, there!" is an instruction.
FILLER = {"um", "uh", "er", "so", "please", "yes", "yeah", "okay", "ok"}


class WakeRouter(Node):
    def __init__(self):
        super().__init__("wake_router")
        p = self.declare_parameter
        prefixes = p("wake_prefixes", ["hey", "hi", "hay", "hello", "ok", "okay", "oi"]).value
        names = p("wake_names", ["roxy", "roxie", "roxi", "rocksy", "roxey"]).value
        loose = p("wake_names_loose", ["rocky", "foxy", "proxy", "rox", "rocks"]).value
        self.stop_words = set(p("stop_words", ["stop", "halt", "freeze"]).value)
        self.greetings = set(p("greet_prefixes", ["hi", "hello"]).value)  # alone: a "hi" request
        self.window = p("followup_window", 8.0).value
        self.forward = p("forward_to_navila", True).value

        # Exact names wake Roxy on their own; sound-alikes need "hey" / "hi" in front.
        pre = "|".join(map(re.escape, prefixes))
        self.wake_re = re.compile(
            rf"\b(?:(?:{pre})\W+)?(?:{'|'.join(map(re.escape, names))})\b"
            rf"|\b(?:{pre})\W+(?:{'|'.join(map(re.escape, loose))})\b", re.IGNORECASE)
        self.not_content = set(prefixes) | set(names) | set(loose) | FILLER
        self.deadline = 0.0  # follow-up window after the wake phrase alone

        self.request_pub = self.create_publisher(String, "stt/request", 10)
        self.instruction_pub = self.create_publisher(String, "navila/instruction", 10)
        self.create_subscription(String, "stt/transcript", self.on_transcript, 10)
        self.create_subscription(Bool, "stt/mute", self.on_mute, LATCHED)
        self.get_logger().info(
            f"waiting for 'Hey Roxy, ...'; requests {'go' if self.forward else 'do not go'} "
            f"to navila/instruction")

    def on_mute(self, msg):
        # Roxy just finished speaking (e.g. "Yes?" after a bare "Hey Roxy"): the follow-up
        # window starts now, not while the mic was muted
        if not msg.data and self.deadline > time.monotonic():
            self.deadline = time.monotonic() + self.window

    def on_transcript(self, msg):
        text = msg.data.strip()
        words = [w for w in re.findall(r"[a-z']+", text.lower()) if w not in self.not_content]

        if words and words[0] in self.stop_words:  # safety first: no wake phrase needed
            self.deadline = 0.0
            self.get_logger().info(f"stop: {text!r}")
            self.instruction_pub.publish(String(data="stop"))
            return

        match = self.wake_re.search(text)
        if match:
            request = f"{text[:match.start()]} {text[match.end():]}"
        elif time.monotonic() < self.deadline:
            request = text
        else:
            return
        self.deadline = 0.0

        if not words:
            self.deadline = time.monotonic() + self.window
            greeting = match is not None and re.match(r"[a-z']+", match.group(0).lower()).group(0) in self.greetings
            self.get_logger().info(f"wake phrase alone{' (a greeting)' if greeting else ''}: "
                                   "the next sentence is the request")
            self.request_pub.publish(String(data="hi" if greeting else ""))  # never to navila
            return

        request = request.strip(" ,.!?;:-")
        self.get_logger().info(f"request: {request!r}")
        self.request_pub.publish(String(data=request))
        if self.forward:
            self.instruction_pub.publish(String(data=request))


def main():
    rclpy.init()
    node = WakeRouter()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
