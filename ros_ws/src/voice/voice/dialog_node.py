"""Spoken replies: stt/request -> Gemini -> a NaVILA command or a spoken answer.

  "Hey Roxy"                      -> a short acknowledgement; wake_router takes the next sentence
  "Hey Roxy, walk to that chair"  -> navigate: navila/instruction "walk to that chair", word for word,
                                     plus a short spoken confirmation
  "Hey Roxy, what's your name?"   -> question: a spoken answer
  navila/instruction "stop"       -> a short spoken "stopping" (wake_router already sent the stop)

Gemini only classifies the request and writes what Roxy says. NaVILA always gets the request as
spoken, because deictic words ("that", "there") go with the pointing gesture. Without Gemini (no
key, no internet) Roxy says so and nothing reaches NaVILA; run with VOICE=false to forward every
request directly instead (wake_router).

stt/mute is true while Roxy speaks (from voice/say until the Pi reports the end of playback), so
the mic never hears Roxy. Parameters: configs/robots/roxy.yaml, dialog_node. Key: GEMINI_API_KEY.
"""

import json
import random
import threading
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String

from voice.personas import ACKS, OFFLINE, PERSONAS, STOPPING

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": ["navigate", "question"]},
        "question": {"type": "STRING"},
        "reply": {"type": "STRING"},
    },
    "required": ["intent", "question", "reply"],
}


class DialogNode(Node):
    def __init__(self):
        super().__init__("dialog_node")
        p = self.declare_parameter
        self.model = p("gemini_model", "gemini-3.5-flash-lite").value
        self.thinking_level = p("thinking_level", "minimal").value
        self.max_words = p("max_words", 35).value
        self.history_turns = p("history_turns", 4).value
        timeout = p("gemini_timeout", 15.0).value
        self.speak_timeout = p("speak_timeout", 25.0).value  # unmute if the Pi never reports playback

        self.client = None
        try:
            from google import genai
            from google.genai import types
            self.types = types
            self.client = genai.Client(http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        except Exception as e:
            self.get_logger().error(f"Gemini disabled ({e}): set GEMINI_API_KEY in .env")

        self.preset = "scooby"
        self.history = []
        self.thinking = False
        self.speak_t0 = None  # set from voice/say until the Pi reports the end of playback
        self.playing = False
        self.muted = None

        self.instruction_pub = self.create_publisher(String, "navila/instruction", 10)
        self.say_pub = self.create_publisher(String, "voice/say", 10)
        self.qa_pub = self.create_publisher(String, "voice/qa", 10)
        self.mute_pub = self.create_publisher(Bool, "stt/mute", LATCHED)
        self.create_subscription(String, "stt/request", self.on_request, 10)
        self.create_subscription(String, "navila/instruction", self.on_instruction, 10)
        self.create_subscription(Bool, "voice/speaking", self.on_speaking, LATCHED)
        self.create_subscription(String, "voice/preset", self.on_preset, LATCHED)
        self.create_timer(0.5, self.watchdog)
        self.set_mute(False)
        self.get_logger().info(f"dialog ready ({self.model})")

    # ---------------- speaking and the mic ----------------
    def set_mute(self, muted):
        if muted != self.muted:
            self.muted = muted
            self.mute_pub.publish(Bool(data=muted))

    def say(self, text):
        self.speak_t0 = time.monotonic()
        self.set_mute(True)
        self.say_pub.publish(String(data=text))

    def on_speaking(self, msg):
        if msg.data:
            self.playing = True
        elif self.playing:
            self.playing = False
            self.done_speaking()

    def done_speaking(self):
        self.speak_t0 = None
        self.set_mute(False)

    def watchdog(self):
        if self.speak_t0 is not None and not self.playing \
                and time.monotonic() - self.speak_t0 > self.speak_timeout:
            self.get_logger().warn("the Pi never reported playback: unmuting the mic")
            self.done_speaking()

    def on_preset(self, msg):
        if msg.data in PERSONAS:
            self.preset = msg.data

    # ---------------- requests ----------------
    def on_instruction(self, msg):
        if msg.data.strip().lower() == "stop":
            self.say(random.choice(STOPPING[self.preset]))

    def on_request(self, msg):
        text = msg.data.strip()
        if not text:
            self.say(random.choice(ACKS[self.preset]))
        elif self.thinking:
            self.get_logger().info(f"still thinking, ignored: {text!r}")
        else:
            self.thinking = True
            threading.Thread(target=self.respond, args=(text,), daemon=True).start()

    def respond(self, text):
        t0 = time.monotonic()
        try:
            out = self.ask_gemini(text)
        except Exception as e:
            self.get_logger().error(f"Gemini failed: {e}")
            out = {"intent": "offline", "question": "", "reply": OFFLINE[self.preset]}
        self.thinking = False

        if out["intent"] == "navigate":
            self.instruction_pub.publish(String(data=text))  # word for word, never Gemini's version
        if out["intent"] != "offline":
            self.history = (self.history + [{"heard": text, **out}])[-self.history_turns:]
        self.get_logger().info(f"{out['intent']}: {text!r} -> {out['reply']!r} "
                               f"({time.monotonic() - t0:.1f} s)")
        self.qa_pub.publish(String(data=json.dumps({"heard": text, **out})))
        self.say(out["reply"])

    def ask_gemini(self, text):
        if self.client is None:
            raise RuntimeError("no API key")
        types = self.types
        contents = []
        for turn in self.history:
            contents.append(types.Content(role="user", parts=[types.Part(text=turn["heard"])]))
            answer = {k: turn[k] for k in ("intent", "question", "reply")}
            contents.append(types.Content(role="model", parts=[types.Part(text=json.dumps(answer))]))
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        config = types.GenerateContentConfig(
            system_instruction=PERSONAS[self.preset].format(max_words=self.max_words),
            response_mime_type="application/json",
            response_schema=SCHEMA,
            temperature=0.7,
            max_output_tokens=512,
        )
        if self.thinking_level:
            try:
                config.thinking_config = types.ThinkingConfig(thinking_level=self.thinking_level)
            except Exception as e:  # older google-genai without thinking_level
                self.get_logger().warn(f"thinking_level not supported here: {e}")
                self.thinking_level = ""
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=contents, config=config)
        except Exception as e:
            if not self.thinking_level or "thinking" not in str(e).lower():
                raise
            self.get_logger().warn(f"{self.model} rejected thinking_level, retrying without it")
            self.thinking_level = ""
            config.thinking_config = None
            response = self.client.models.generate_content(
                model=self.model, contents=contents, config=config)

        out = json.loads(response.text)
        intent = out.get("intent") if out.get("intent") in ("navigate", "question") else "question"
        reply = str(out.get("reply", "")).strip() or random.choice(ACKS[self.preset])
        return {"intent": intent, "question": str(out.get("question", "")).strip(), "reply": reply}


def main():
    rclpy.init()
    node = DialogNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
