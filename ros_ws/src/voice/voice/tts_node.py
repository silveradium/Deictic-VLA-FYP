"""Text -> Roxy's voice: voice/say -> Piper TTS + effects -> voice/audio (one WAV per line).

Roxy's voice is a named voice (voice_library.py: scooby, robot and 100 robot voices), set by the
voice_preset parameter (`make voice-preset PRESET=poppy-bright` while running), or a custom spec
(voice_fx.py) on voice/config from the dashboard's Voice tab, which also remembers it across
restarts. The voice's persona is published on voice/preset, so dialog_node talks in the matching
style. voice/speak ({"text": ..., "voice": spec or null}) says one line, in the given voice or
Roxy's own, without changing Roxy's voice: the dashboard's voice tests and typed lines. The Pi's
speaker_node plays the clips. Parameters: configs/robots/roxy.yaml, tts_node.
"""

import array
import json
import time

import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String, UInt8MultiArray

from voice import voice_fx
from voice.voice_library import LIBRARY, spec_of

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)


class TtsNode(Node):
    def __init__(self):
        super().__init__("tts_node")
        preset = self.declare_parameter("voice_preset", "scooby").value
        fallback = self.declare_parameter("piper_voice", "en_US-ryan-medium").value
        voices_dir = self.declare_parameter("piper_voices_dir", "/opt/piper-voices").value
        if preset not in LIBRARY:
            raise ValueError(f"voice_preset {preset!r} is not in voice_library.py (e.g. scooby, robot, poppy-bright)")

        self.voices = voice_fx.Voices(voices_dir, fallback)
        self.audio_pub = self.create_publisher(UInt8MultiArray, "voice/audio", 10)
        self.preset_pub = self.create_publisher(String, "voice/preset", LATCHED)
        self.set_voice(spec_of(preset), preset)
        self.create_subscription(String, "voice/say", lambda m: self.speak(m.data, self.spec), 10)
        self.create_subscription(String, "voice/speak", self.on_speak, 10)
        self.create_subscription(String, "voice/config", self.on_config, LATCHED)
        self.add_on_set_parameters_callback(self.on_params)
        self.get_logger().info(f"voice ready: {preset!r}; {len(self.voices.installed())} Piper models installed")

    def set_voice(self, spec, name):
        self.spec = voice_fx.full(spec)
        self.voices.get(self.spec["model"])  # load now, not on the first reply
        self.preset_pub.publish(String(data=self.spec["persona"]))
        self.get_logger().info(f"Roxy's voice -> {name} ({self.spec['model']} #{self.spec['speaker']}, "
                               f"pitch {self.spec['pitch']:+d}, persona {self.spec['persona']})")

    def on_params(self, params):
        for param in params:
            if param.name == "voice_preset":
                if param.value not in LIBRARY:
                    return SetParametersResult(successful=False, reason="not in voice_library.py")
                self.set_voice(spec_of(param.value), param.value)
        return SetParametersResult(successful=True)

    def on_config(self, msg):
        try:
            cfg = json.loads(msg.data)
            self.set_voice(cfg["spec"], cfg.get("label", "custom"))
        except (ValueError, KeyError, TypeError) as e:
            self.get_logger().error(f"bad voice/config {msg.data[:200]!r}: {e}")

    def on_speak(self, msg):
        try:
            req = json.loads(msg.data)
            self.speak(str(req["text"]), req.get("voice") or self.spec)
        except (ValueError, KeyError, TypeError) as e:
            self.get_logger().error(f"bad voice/speak {msg.data[:200]!r}: {e}")

    def speak(self, text, spec):
        t0 = time.monotonic()
        try:
            wav = voice_fx.render(self.voices, text, spec)
        except Exception as e:  # a bad custom voice must not take Roxy's voice down
            self.get_logger().error(f"could not render {text!r}: {e}")
            return
        if wav is None:
            return
        self.audio_pub.publish(UInt8MultiArray(data=array.array("B", wav)))
        rate = int.from_bytes(wav[24:28], "little")
        seconds = (len(wav) - 44) / 2 / rate
        self.get_logger().info(
            f"{text!r} -> {seconds:.1f} s of audio in {time.monotonic() - t0:.2f} s")


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
