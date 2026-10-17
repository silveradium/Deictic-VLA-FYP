"""Piper speech -> Roxy's voice.

A voice is a dict (a "spec", every key optional, DEFAULTS fill in the rest): which Piper model and
speaker says the words, then effects on top. voice_library.py has the named ones (scooby, robot and
100 robot voices); the dashboard's Voice tab edits a spec live.

  model, speaker    Piper voice (/opt/piper-voices/<model>.onnx) and speaker id for multi-speaker models
  speed             Piper length_scale: > 1 speaks slower
  pitch             cents (100 = a semitone up); high voices carry better over a noisy room
  ring_hz, ring_mix ring modulation, the classic metallic robot (0 Hz = off)
  bits              bit-crush to this many bits (0 = off; 6 sounds 8-bit)
  overdrive, bass   dB
  flanger, chorus   on/off
  echo_ms           a short echo (~6 ms is a tin-can comb; 0 = off)
  tremolo_hz/_depth wobble (0 Hz = off)
  punch             cut through noise: drop the boom, lift 2-4 kHz, compress so quiet words stay loud
  persona           which personality dialog_node uses with this voice (voice/personas.py)
"""

import io
import os
import subprocess
import threading
import wave
from collections import OrderedDict

import numpy as np
from piper import PiperVoice, SynthesisConfig

DEFAULTS = {
    "model": "en_US-ryan-medium", "speaker": 0, "speed": 1.0, "pitch": 0,
    "ring_hz": 0, "ring_mix": 0.5, "bits": 0, "overdrive": 0.0, "bass": 0.0,
    "flanger": False, "chorus": False, "echo_ms": 0, "tremolo_hz": 0.0, "tremolo_depth": 30,
    "punch": False, "persona": "robot",
}

LEAD_IN = 0.25       # s of silence first: USB speakers often swallow the start of a clip
SENTENCE_GAP = 0.15  # s between Piper's per-sentence chunks

# punch: high-pass the boom a small speaker can't play anyway, presence boost where speech is
# understood, then a compressor that raises the quiet syllables
PUNCH = ["highpass", "180", "equalizer", "3000", "1.2q", "+6",
         "compand", "0.002,0.08", "6:-70,-70,-40,-12,-20,-6,0,-3", "0", "-90", "0.005"]


def full(spec):
    """spec with every key, unknown keys dropped and numbers clamped to sane ranges."""
    s = {**DEFAULTS, **{k: v for k, v in (spec or {}).items() if k in DEFAULTS}}
    clamp = {"speaker": (0, 1000), "speed": (0.5, 2.0), "pitch": (-1200, 1600), "ring_hz": (0, 400),
             "ring_mix": (0, 1), "bits": (0, 16), "overdrive": (0, 30), "bass": (-20, 20), "echo_ms": (0, 200),
             "tremolo_hz": (0, 20), "tremolo_depth": (0, 100)}
    for k, (lo, hi) in clamp.items():
        s[k] = type(DEFAULTS[k])(min(hi, max(lo, float(s[k]))))
    for k in ("flanger", "chorus", "punch"):
        s[k] = bool(s[k])
    s["model"] = os.path.basename(str(s["model"]))  # a file name in voices_dir, nothing else
    return s


def sox_effects(s):
    fx = []
    if s["pitch"]:
        fx += ["pitch", str(s["pitch"])]
    if s["overdrive"]:
        fx += ["overdrive", str(s["overdrive"]), "20"]
    if s["bass"]:
        fx += ["bass", f"{s['bass']:+g}"]
    if s["flanger"]:
        fx += ["flanger"]
    if s["chorus"]:
        fx += ["chorus", "0.6", "0.9", "50", "0.4", "0.25", "2", "-t"]
    if s["echo_ms"]:
        fx += ["echo", "0.8", "0.88", str(s["echo_ms"]), "0.4"]
    if s["tremolo_hz"]:
        fx += ["tremolo", str(s["tremolo_hz"]), str(s["tremolo_depth"])]
    if s["punch"]:
        fx += PUNCH
    return fx + ["gain", "-n", "-1"]


class Voices:
    """Piper models by name, loaded on first use; only the last `keep` stay in memory (~120 MB each)."""

    def __init__(self, voices_dir, fallback="en_US-ryan-medium", keep=3):
        self.dir, self.fallback, self.keep = voices_dir, fallback, keep
        self.loaded = OrderedDict()
        self.lock = threading.Lock()

    def installed(self):
        return sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".onnx"))

    def get(self, model):
        """(PiperVoice, model name actually used): a model that isn't installed falls back."""
        if not os.path.isfile(os.path.join(self.dir, model + ".onnx")):
            model = self.fallback
        with self.lock:
            if model not in self.loaded:
                self.loaded[model] = PiperVoice.load(os.path.join(self.dir, model + ".onnx"))
                while len(self.loaded) > self.keep:
                    self.loaded.popitem(last=False)
            self.loaded.move_to_end(model)
            return self.loaded[model], model


def render(voices, text, spec):
    """A complete 16-bit mono WAV file (bytes), or None for empty text."""
    s = full(spec)
    voice, _ = voices.get(s["model"])
    speakers = voice.config.num_speakers
    syn = SynthesisConfig(length_scale=s["speed"], speaker_id=s["speaker"] % speakers if speakers > 1 else None)
    chunks = list(voice.synthesize(text, syn_config=syn))
    if not chunks:
        return None
    rate = chunks[0].sample_rate
    gap = np.zeros(int(SENTENCE_GAP * rate), dtype=np.float32)
    audio = np.concatenate([part for c in chunks for part in (c.audio_float_array, gap)])

    if s["ring_hz"]:
        carrier = np.sin(2 * np.pi * s["ring_hz"] * np.arange(len(audio)) / rate)
        audio = (1 - s["ring_mix"]) * audio + s["ring_mix"] * audio * carrier
    if s["bits"]:
        levels = 2 ** (s["bits"] - 1)
        audio = np.round(audio * levels) / levels

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    raw = ["-t", "raw", "-e", "signed", "-b", "16", "-c", "1", "-r", str(rate)]
    pcm = subprocess.run(["sox", "-q", *raw, "-", *raw, "-", *sox_effects(s)],
                         input=pcm, capture_output=True, check=True).stdout

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(int(LEAD_IN * rate) * 2) + pcm)
    return buf.getvalue()
