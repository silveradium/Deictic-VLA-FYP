"""Plays Roxy's spoken replies on the Pi speaker: voice/audio (one WAV file per line) -> aplay.

voice/speaking (latched) is true from the first clip until the queue is empty; dialog_node mutes
the mic meanwhile (stt/mute), so Roxy never transcribes itself. Parameters:
configs/robots/roxy.yaml, speaker_node (alsa_device: see `aplay -L`). gain_db boosts past the
card's own maximum (soft-limited, so loud peaks round off instead of clipping); change it while
it runs with `make pi-volume GAIN=8`.
"""

import io
import queue
import subprocess
import threading
import wave

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, UInt8MultiArray

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)
KNEE = 0.5  # soft limiter: linear below this fraction of full scale, tanh above, never past 1


def boost(wav, gain_db):
    """The WAV file louder by gain_db, peaks soft-limited. Unchanged if gain_db is 0 or not 16-bit."""
    with wave.open(io.BytesIO(wav)) as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    if not gain_db or params.sampwidth != 2:
        return wav
    x = np.frombuffer(frames, np.int16).astype(np.float32) / 32768 * 10 ** (gain_db / 20)
    over = np.abs(x) > KNEE
    x[over] = np.sign(x[over]) * (KNEE + (1 - KNEE) * np.tanh((np.abs(x[over]) - KNEE) / (1 - KNEE)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams(params)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


class SpeakerNode(Node):
    def __init__(self):
        super().__init__("speaker_node")
        self.device = self.declare_parameter("alsa_device", "plughw:CARD=Device,DEV=0").value
        self.declare_parameter("gain_db", 0.0)  # read per clip: `ros2 param set` takes effect at once

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
                wav = boost(wav, self.get_parameter("gain_db").value)
                subprocess.run(["aplay", "-q", "-D", self.device, "-"], input=wav, check=True, timeout=120)
            except (subprocess.SubprocessError, OSError, wave.Error, EOFError) as e:
                self.get_logger().error(f"playback failed: {e}")
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
