"""Mic -> Moonshine speech-to-text -> stt/transcript, one message per finished sentence.

Runs in the bringup container on the machine the DJI Mic Mini receiver is plugged into
(docs/network.md). Moonshine runs on the CPU; the first start downloads the model into the
stt_models volume (docker/compose/ros.yml). Parameters: configs/robots/roxy.yaml, stt_node.

Mics the container can see:
  python3 -c "import sounddevice; print(sounddevice.query_devices())"
"""

import rclpy
from moonshine_voice import string_to_model_arch
from moonshine_voice.mic_transcriber import MicTranscriber
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String


class SttNode(Node):
    def __init__(self):
        super().__init__("stt_node")
        # a substring of the device name, an index, "pulse" or "default"
        device = str(self.declare_parameter(
            "mic_device", "MINI", ParameterDescriptor(dynamic_typing=True)).value)
        arch = self.declare_parameter("model_arch", "medium-streaming").value
        keyterms = self.declare_parameter("keyterms", ["Roxy", "Hey Roxy"]).value

        self.pub = self.create_publisher(String, "stt/transcript", 10)

        if device in ("", "default"):
            device = None
        elif device.isdigit():
            device = int(device)
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
        self.get_logger().info(f"listening on mic {'default' if device is None else device!r}")

    def on_line(self, line):
        # Moonshine's worker thread, once per finished sentence
        text = line.text.strip()
        if text:
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
