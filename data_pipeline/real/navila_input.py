"""What NaVILA is shown: rules the training data and inference must share.

Values come from the navila_node block of configs/robots/roxy.yaml, read by both sides: a model
fine-tuned with one setting expects the same at inference, so change them only together with a
re-export (and re-train). navila_node can't import this module (data_pipeline is not in its
container) and repeats the rules: navila_node.to_navila_view and NavilaNode.gesture_frames.

- image_mode: NaVILA squares every frame. crop = centre square, squash = whole frame squeezed. With
  crop, a person pointing near the side of the frame is cut off (make preview draws the box).
- Pointing frames: the gesture is made while the instruction is given, before Roxy moves, but NaVILA's
  history only grows while moving. So when an instruction arrives, the frames nearest gesture_frames
  times spread over the gesture_window_s seconds before it go at the front of the history (0 = off).
  In a collected episode the instruction "arrives" at t_go, the second Enter.

Pure Python (PyYAML for the config).
"""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

ROXY_YAML = Path(__file__).resolve().parents[2] / "configs" / "robots" / "roxy.yaml"
DEFAULTS = {"image_mode": "crop", "gesture_frames": 4, "gesture_window_s": 3.0}  # = navila_node's
MAX_GAP_S = 0.5  # no frame this close to a target time (camera gap, recording not started): skipped


def load_params(path: Path = ROXY_YAML) -> dict:
    """image_mode, gesture_frames, gesture_window_s from roxy.yaml; DEFAULTS for keys not set there."""
    import yaml

    node = (yaml.safe_load(Path(path).read_text()) or {}).get("navila_node", {}).get("ros__parameters", {})
    return {k: node.get(k, v) for k, v in DEFAULTS.items()}


def gesture_targets(t_end: float, window_s: float, n: int) -> list[float]:
    """n times spread over the window_s seconds before t_end, oldest first. The last is window_s / n
    before t_end: the frame at t_end is the current observation, added by the first decision itself."""
    return [t_end - window_s * (n - k) / n for k in range(n)]


def pick_gesture(times, t_end: float, window_s: float, n: int) -> list[int]:
    """Indices into times (sorted) of the pointing frames: per target, the nearest frame before t_end
    within MAX_GAP_S. Fewer than n when frames are missing; never the same frame twice."""
    hi = bisect_left(times, t_end)
    picked = []
    for target in gesture_targets(t_end, window_s, n):
        i = bisect_left(times, target, 0, hi)
        near = min((j for j in (i - 1, i) if 0 <= j < hi), key=lambda j: abs(times[j] - target), default=None)
        if near is not None and abs(times[near] - target) <= MAX_GAP_S and near not in picked:
            picked.append(near)
    return picked


def crop_box(width: int, height: int, image_mode: str) -> tuple[float, float, float, float]:
    """(left, top, width, height) of the part of a frame NaVILA keeps, as fractions of the frame."""
    if image_mode != "crop":
        return 0.0, 0.0, 1.0, 1.0
    s = min(width, height)
    return (width - s) / 2 / width, (height - s) / 2 / height, s / width, s / height
