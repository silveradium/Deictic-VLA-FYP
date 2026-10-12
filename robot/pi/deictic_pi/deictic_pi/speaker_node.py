"""Plays Roxy's spoken replies on the Pi speaker: voice/audio (one WAV file per line) -> aplay.

voice/speaking (latched) is true from the first clip until the queue is empty; dialog_node mutes
the mic meanwhile (stt/mute), so Roxy never transcribes itself. Parameters:
configs/robots/roxy.yaml, speaker_node (alsa_device: see `aplay -L`).
"""

import queue
import subprocess
import threading

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, UInt8MultiArray

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)


class SpeakerNode(Node):
    def __init__(self):
        super().__init__("speaker_node")
        self.device = self.declare_parameter("alsa_device", "plughw:CARD=Device,DEV=0").value

        self.speaking_pub = self.create_publisher(Bool, "voice/speaking", LATCHED)
        self.create_subscription(UInt8MultiArray, "voice/audio", self.on_audio, 10)
        self.clips = queue.Queue()
        self.speaking = False
        self.speaking_pub.publish(Bool(data=False))
        threading.Thread(target=self.play_loop, daemon=True).start()
        self.get_logger().info(f"speaker ready on {self.device}")

    def on_audio(self, msg):
        wav = msg.data.tobytes() if hasattr(msg.data, "tobytes") else bytes(msg.data)
        self.get_logger().info(f"clip: {len(wav) / 1024:.0f} KB")
        self.clips.put(wav)

    def set_speaking(self, speaking):
        self.speaking = speaking
        self.speaking_pub.publish(Bool(data=speaking))

    def play_loop(self):
        while True:
            wav = self.clips.get()
            if not self.speaking:
                self.set_speaking(True)
            try:  # plughw converts the clip's rate and channels to what the card takes
                subprocess.run(["aplay", "-q", "-D", self.device, "-"], input=wav, check=True, timeout=120)
            except (subprocess.SubprocessError, OSError) as e:
                self.get_logger().error(f"aplay failed: {e}")
            if self.clips.empty():
                self.set_speaking(False)


def main():
    rclpy.init()
    node = SpeakerNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
