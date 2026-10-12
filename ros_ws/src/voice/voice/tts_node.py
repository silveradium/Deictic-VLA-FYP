"""Text -> Roxy's voice: voice/say -> Piper TTS + effect preset -> voice/audio (one WAV per line).

Presets (voice_fx.py): scooby (deeper, gravelly, wobbly cartoon dog) and robot (ring-modulated,
metallic). Switch while running with `make voice-preset PRESET=robot`. The current preset is
published on voice/preset, so dialog_node talks in the matching style. The Pi's speaker_node
plays the clips. Parameters: configs/robots/roxy.yaml, tts_node.
"""

import array
import os
import time

import rclpy
from piper import PiperVoice
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String, UInt8MultiArray

from voice import voice_fx

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)


class TtsNode(Node):
    def __init__(self):
        super().__init__("tts_node")
        self.preset = self.declare_parameter("voice_preset", "scooby").value
        voice = self.declare_parameter("piper_voice", "en_US-ryan-medium").value
        voices_dir = self.declare_parameter("piper_voices_dir", "/opt/piper-voices").value
        if self.preset not in voice_fx.PRESETS:
            raise ValueError(f"voice_preset must be one of {list(voice_fx.PRESETS)}")

        self.voice = PiperVoice.load(os.path.join(voices_dir, voice + ".onnx"))
        self.audio_pub = self.create_publisher(UInt8MultiArray, "voice/audio", 10)
        self.preset_pub = self.create_publisher(String, "voice/preset", LATCHED)
        self.create_subscription(String, "voice/say", self.on_say, 10)
        self.add_on_set_parameters_callback(self.on_params)
        self.preset_pub.publish(String(data=self.preset))
        self.get_logger().info(f"voice ready: Piper {voice}, preset {self.preset!r}")

    def on_params(self, params):
        for param in params:
            if param.name == "voice_preset":
                if param.value not in voice_fx.PRESETS:
                    return SetParametersResult(
                        successful=False, reason=f"choose one of {list(voice_fx.PRESETS)}")
                self.preset = param.value
                self.preset_pub.publish(String(data=self.preset))
                self.get_logger().info(f"voice preset -> {self.preset}")
        return SetParametersResult(successful=True)

    def on_say(self, msg):
        t0 = time.monotonic()
        wav = voice_fx.render(self.voice, msg.data, self.preset)
        if wav is None:
            return
        self.audio_pub.publish(UInt8MultiArray(data=array.array("B", wav)))
        seconds = (len(wav) - 44) / 2 / self.voice.config.sample_rate
        self.get_logger().info(
            f"{msg.data!r} -> {seconds:.1f} s of audio in {time.monotonic() - t0:.2f} s")


def main():
    rclpy.init()
    node = TtsNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
