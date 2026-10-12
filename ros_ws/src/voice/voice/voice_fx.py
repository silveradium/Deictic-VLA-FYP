"""Piper speech -> Roxy's voice. Presets:

  scooby  deeper, gravelly, wobbly cartoon dog
  robot   ring-modulated, metallic robo-dog

Tune by editing the sox effect lists (`man sox`, EFFECTS).
"""

import io
import subprocess
import wave

import numpy as np
from piper import SynthesisConfig

PRESETS = {
    "scooby": {
        "length_scale": 1.08,  # > 1 speaks slower
        "ring_hz": 0,
        "sox": ["pitch", "-300", "overdrive", "8", "20", "tremolo", "5.5", "30",
                "bass", "+5", "gain", "-n", "-1"],
    },
    "robot": {
        "length_scale": 0.95,
        "ring_hz": 45, "ring_mix": 0.65,
        "sox": ["pitch", "-150", "flanger", "echo", "0.8", "0.88", "6", "0.4", "gain", "-n", "-1"],
    },
}

LEAD_IN = 0.25       # s of silence first: USB speakers often swallow the start of a clip
SENTENCE_GAP = 0.15  # s between Piper's per-sentence chunks


def render(voice, text, preset):
    """A complete 16-bit mono WAV file (bytes), or None for empty text."""
    p = PRESETS[preset]
    chunks = list(voice.synthesize(text, syn_config=SynthesisConfig(length_scale=p["length_scale"])))
    if not chunks:
        return None
    rate = chunks[0].sample_rate
    gap = np.zeros(int(SENTENCE_GAP * rate), dtype=np.float32)
    audio = np.concatenate([part for c in chunks for part in (c.audio_float_array, gap)])

    if p["ring_hz"]:
        carrier = np.sin(2 * np.pi * p["ring_hz"] * np.arange(len(audio)) / rate)
        audio = (1 - p["ring_mix"]) * audio + p["ring_mix"] * audio * carrier

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    raw = ["-t", "raw", "-e", "signed", "-b", "16", "-c", "1", "-r", str(rate)]
    pcm = subprocess.run(["sox", "-q", *raw, "-", *raw, "-", *p["sox"]],
                         input=pcm, capture_output=True, check=True).stdout

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(int(LEAD_IN * rate) * 2) + pcm)
    return buf.getvalue()
