"""Mic -> Moonshine speech-to-text -> stt/transcript, one message per finished sentence.

Runs in the bringup container on the machine the mic is on: the DJI Mic Mini receiver on USB
(MIC=dji in .env) or on the audio jack (MIC=jack), or the laptop's built-in mic (MIC=builtin)
(docs/network.md). The jack is the laptop's own sound card, which records from the jack only when it
detects a mic there (a TRRS plug); `make mic-check` says which. A mic that isn't there falls back
to the default one, with an error in the log, rather than leaving Roxy deaf. Moonshine runs on the CPU; the first start downloads the model into the
stt_models volume (docker/compose/ros.yml). Parameters: configs/robots/roxy.yaml, stt_node.

While stt/mute is true (Roxy speaking, voice:=true), and for unmute_delay s after, the mic is
ignored, so Roxy never transcribes its own voice.

Mics the container can see:
  python3 -c "import sounddevice; print(sounddevice.query_devices())"
"""

import time

import rclpy
import sounddevice
from moonshine_voice import string_to_model_arch
from moonshine_voice.mic_transcriber import MicTranscriber
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)


class SttNode(Node):
    def __init__(self):
        super().__init__("stt_node")
        # mic:=dji|jack|builtin picks mic_device_<mic>; mic_device, when set, overrides them.
        # Each is a substring of the device name, an index, "pulse" or "default"
        mic = self.declare_parameter("mic", "dji").value
        presets = {
            "dji": self.declare_parameter("mic_device_dji", "MINI").value,
            "jack": self.declare_parameter("mic_device_jack", "default").value,
            "builtin": self.declare_parameter("mic_device_builtin", "default").value,
        }
        if mic not in presets:
            raise ValueError(f"mic must be one of {sorted(presets)}, not {mic!r}")
        device = str(self.declare_parameter(
            "mic_device", "", ParameterDescriptor(dynamic_typing=True)).value) or str(presets[mic])
        arch = self.declare_parameter("model_arch", "medium-streaming").value
        keyterms = self.declare_parameter("keyterms", ["Roxy", "Hey Roxy"]).value
        self.unmute_delay = self.declare_parameter("unmute_delay", 0.5).value

        self.pub = self.create_publisher(String, "stt/transcript", 10)
        self.mute_requested = False
        self.muted = False
        self.last_mute_t = 0.0
        self.create_subscription(Bool, "stt/mute", self.on_mute, LATCHED)

        if device in ("", "default"):
            device = None
        elif device.isdigit():
            device = int(device)
        if device is not None:
            try:
                sounddevice.query_devices(device, "input")
            except ValueError as e:  # e.g. MIC=dji with the receiver on the jack: no USB "MINI"
                self.get_logger().error(f"no input device {device!r} for mic:={mic} ({e}); "
                                        "using the default mic instead (make mic-check)")
                device = None
        # The device's own sample rate (48 kHz for the DJI) is used if it refuses 16 kHz;
        # Moonshine resamples.
        self.mic = (MicTranscriber()
                    .device(device)
                    .model_arch(string_to_model_arch(arch))
                    .on_line(self.on_line)
                    .on_error(lambda e: self.get_logger().error(f"mic: {e}")))
        self.get_logger().info(f"loading Moonshine {arch} (the first start downloads it) ...")
        self.mic.load()
        if keyterms:
            try:
                self.mic.set_keyterms(keyterms)  # biases the decoder towards the robot's name
            except Exception as e:  # only the streaming models take key terms
                self.get_logger().warn(f"keyterms not applied: {e}")
        self.mic.start()
        self.create_timer(0.1, self.update_mute)
        self.get_logger().info(f"listening on mic {'default' if device is None else device!r}")

    def on_mute(self, msg):
        self.mute_requested = msg.data

    def update_mute(self):
        now = time.monotonic()
        if self.mute_requested:
            self.last_mute_t = now
        muted = now - self.last_mute_t < self.unmute_delay  # a tail for the speaker's echo
        if muted != self.muted:
            self.muted = muted
            self.mic.mute(muted)
            self.get_logger().info("mic muted" if muted else "mic live")

    def on_line(self, line):
        # Moonshine's worker thread, once per finished sentence
        text = line.text.strip()
        if text and not self.muted:
            self.get_logger().info(f"heard: {text!r}")
            self.pub.publish(String(data=text))

    def destroy_node(self):
        self.mic.close()
        super().destroy_node()


def main():
    rclpy.init()
    node = SttNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
