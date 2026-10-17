"""Roxy's named voices: the two originals (scooby, robot) and 100 robot voices = 20 Piper speakers x
5 robot styles, numbered 1-100 in the dashboard's Voice tab (1-5 Ryan, 6-10 Amy, ...). Every style
has punch on and a raised pitch, for a noisy hall. The Piper models are baked into the ROS image
(docker/ros/Dockerfile); a voice whose model is missing falls back to en_US-ryan-medium.
"""

# Piper speakers: (label, model, speaker id, how much of a style's pitch rise it gets). Female voices
# start higher, so they get less of the rise before they squeak.
BASES = [
    ("Ryan", "en_US-ryan-medium", 0, 1.0),
    ("Amy", "en_US-amy-medium", 0, 0.6),
    ("Lessac", "en_US-lessac-medium", 0, 0.6),
    ("Kristin", "en_US-kristin-medium", 0, 0.6),
    ("Hazel", "en_US-hfc_female-medium", 0, 0.6),
    ("Jenny", "en_GB-jenny_dioco-medium", 0, 0.6),
    ("Prudence", "en_GB-semaine-medium", 0, 0.6),
    ("Spike", "en_GB-semaine-medium", 1, 1.0),
    ("Obadiah", "en_GB-semaine-medium", 2, 1.0),
    ("Poppy", "en_GB-semaine-medium", 3, 0.6),
    ("Alan", "en_US-arctic-medium", 0, 1.0),     # awb
    ("Rex", "en_US-arctic-medium", 1, 1.0),      # rms
    ("Sally", "en_US-arctic-medium", 2, 0.6),    # slt
    ("Clara", "en_US-arctic-medium", 4, 0.6),    # clb
    ("Lena", "en_US-arctic-medium", 5, 0.6),     # lnh
    ("Ben", "en_US-arctic-medium", 7, 1.0),      # bdl
    ("Isla", "en_GB-vctk-medium", 0, 0.6),       # p239
    ("Fiona", "en_GB-vctk-medium", 1, 0.6),      # p236
    ("Hamish", "en_GB-vctk-medium", 4, 1.0),     # p259
    ("Owen", "en_GB-vctk-medium", 7, 1.0),       # p263
]

# Robot styles; "pitch" is scaled by the speaker's factor above
STYLES = {
    "Clear": {"pitch": 300, "ring_hz": 110, "ring_mix": 0.2, "speed": 0.95},  # most intelligible
    "Bright": {"pitch": 450, "flanger": True, "speed": 0.95},
    "Metal": {"pitch": 250, "ring_hz": 55, "ring_mix": 0.45, "echo_ms": 6, "speed": 0.95},
    "Pixel": {"pitch": 500, "bits": 6, "speed": 0.92},
    "Droid": {"pitch": 800, "chorus": True, "tremolo_hz": 7.0, "tremolo_depth": 25, "speed": 0.9},
}

ORIGINALS = {
    "scooby": {"model": "en_US-ryan-medium", "speed": 1.08, "pitch": -300, "overdrive": 8.0,
               "tremolo_hz": 5.5, "tremolo_depth": 30, "bass": 5.0, "persona": "scooby"},
    "robot": {"model": "en_US-ryan-medium", "speed": 0.95, "pitch": -150, "ring_hz": 45, "ring_mix": 0.65,
              "flanger": True, "echo_ms": 6, "persona": "robot"},
}


def _build():
    voices = {}
    n = 0
    for label, model, speaker, lift in BASES:  # speaker by speaker: next/prev keeps the model loaded
        for style, fx in STYLES.items():
            n += 1
            voices[f"{label.lower()}-{style.lower()}"] = {
                "n": n, "label": f"{label} {style}", "style": style,
                "spec": {"model": model, "speaker": speaker, "punch": True, "persona": "robot",
                         **fx, "pitch": round(fx["pitch"] * lift)}}
    return voices


LIBRARY = {name: {"n": 0, "label": name.capitalize(), "style": "Original", "spec": spec}
           for name, spec in ORIGINALS.items()} | _build()

MODELS = sorted({model for _, model, _, _ in BASES})


def spec_of(name):
    """The spec of a named voice, KeyError for an unknown name."""
    return dict(LIBRARY[name]["spec"])
