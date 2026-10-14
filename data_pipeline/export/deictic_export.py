"""Our collected episodes -> NaVILA's training format (the same as R2R / RxR in a8cheng/NaVILA-Dataset).

Per kept episode (sessions/session_*/ep_*/: bag + episode.json from `make collect`), one record per
decision, NaVILA's answer for it, and the history it was taken with:

  {"video_id": "<session>_<ep>-<step>", "q": instruction, "a": answer, "frames": ["<uid>/0000.jpg", ...]}

The history is what navila_node gives NaVILA at inference, built by the same rules
(data_pipeline/real/navila_input.py, values from configs/robots/roxy.yaml):
  - the pointing frames from just before t_go (the second Enter, when the instruction was given),
  - the frame at t_go,
  - then one frame per 25 cm / 15 degree unit, taken when odometry says the unit was completed
    (as in NaVILA's data, checked in mix_navila.ipynb step 1).
Frames are squared like navila_node.to_navila_view (OpenCV, image_mode, 512 px).

Stop records are repeated so stops make up the same share of the records as in the replay data.
NaVILA repeats each stop 3x (R2R) or 5x (RxR), but its trajectories are 25-40 steps long; ours are a
few steps, so the same repeat would make stop several times more frequent than NaVILA ever saw.

Runs without ROS: rosbags reads the mcap bags.
"""

from __future__ import annotations

import json
import math
import random
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from data_pipeline.export.navila_data import units
from data_pipeline.real.navila_input import MAX_GAP_S, pick_gesture
from data_pipeline.real.odom_actions import STOP, Action, merge_units, odom_to_units

CAMERA = "/orbbec/color/image_raw/compressed"
ODOM = "/odom"
VIEW_SIZE = 512  # navila_node.to_navila_view


def yaw_deg(q) -> float:
    return math.degrees(math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)))


def read_bag(bag_dir: Path):
    """Camera JPEGs [(t, bytes)] and odometry [(t, x, y, yaw_deg)], t = bag time (recorder receive time,
    the clock of episode.json)."""
    from rosbags.highlevel import AnyReader
    from rosbags.typesys import Stores, get_typestore

    frames, odom = [], []
    with AnyReader([Path(bag_dir)], default_typestore=get_typestore(Stores.ROS2_HUMBLE)) as reader:
        conns = [c for c in reader.connections if c.topic in (CAMERA, ODOM)]
        for conn, t_ns, raw in reader.messages(connections=conns):
            msg = reader.deserialize(raw, conn.msgtype)
            t = t_ns * 1e-9
            if conn.topic == CAMERA:
                frames.append((t, bytes(msg.data)))
            else:
                p = msg.pose.pose.position
                odom.append((t, p.x, p.y, yaw_deg(msg.pose.pose.orientation)))
    return frames, odom


def navila_view(jpeg: bytes, image_mode: str, size: int = VIEW_SIZE) -> np.ndarray:
    """JPEG -> square RGB frame, as navila_node.to_navila_view does it at inference."""
    rgb = cv2.cvtColor(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
    if image_mode == "crop":
        h, w = rgb.shape[:2]
        s = min(h, w)
        rgb = rgb[(h - s) // 2: (h + s) // 2, (w - s) // 2: (w + s) // 2]
    return cv2.resize(rgb, (size, size), interpolation=cv2.INTER_AREA)


def nearest(times, t) -> int:
    i = bisect_left(times, t)
    return min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - t))


@dataclass
class Plan:
    """One episode, before anything is written."""
    uid: str
    instruction: str = ""
    actions: list = field(default_factory=list)   # merged actions, stop last
    history: list = field(default_factory=list)   # JPEG bytes, in history order
    n_pointing: int = 0
    problems: list = field(default_factory=list)


def plan_episode(ep_dir: Path, uid: str, params: dict) -> Plan:
    plan = Plan(uid)
    meta = json.loads((ep_dir / "episode.json").read_text()) if (ep_dir / "episode.json").exists() else {}
    plan.instruction = (meta.get("instruction") or "").strip()
    if not meta.get("kept"):
        plan.problems.append("not kept (no episode.json or kept false)")
    if not plan.instruction:
        plan.problems.append("no instruction")
    if not (ep_dir / "metadata.yaml").exists():
        plan.problems.append("bag not closed (no metadata.yaml): ros2 bag reindex <dir> mcap")
    if plan.problems:
        return plan

    frames, odom = read_bag(ep_dir)
    t_go = meta["t_go"]
    driving = [p for p in odom if p[0] >= t_go]
    unit_actions, stats = odom_to_units(driving)
    plan.problems += stats.warnings
    if not frames:
        plan.problems.append("no camera frames")
    if not frames or not driving:
        return plan
    plan.actions = merge_units(unit_actions) + [Action(STOP, 0.0, driving[-1][0])]

    times = [t for t, _ in frames]
    pointing = pick_gesture(times, t_go, params["gesture_window_s"], params["gesture_frames"])
    plan.n_pointing = len(pointing)
    if len(pointing) < params["gesture_frames"]:
        plan.problems.append(f"{len(pointing)} of {params['gesture_frames']} pointing frames")
    observed = []
    for t in [t_go] + [u.t for u in unit_actions]:
        i = nearest(times, t)
        if abs(times[i] - t) > MAX_GAP_S:
            plan.problems.append(f"no frame within {MAX_GAP_S} s of {t - t_go:+.1f} s after go")
        observed.append(i)
    plan.history = [frames[i][1] for i in pointing + observed]
    return plan


def records(plan: Plan, stop_repeat: int) -> list[dict]:
    """The episode's decision records: history = pointing frames, frame at go, one frame per unit done."""
    out, done = [], 0
    for k, a in enumerate(plan.actions):
        n = plan.n_pointing + 1 + done
        rec = {"video_id": f"{plan.uid}-{k}", "q": plan.instruction, "a": a.answer(),
               "frames": [f"{plan.uid}/{p:04d}.jpg" for p in range(n)]}
        out += [rec] * (stop_repeat if a.kind == STOP else 1)
        done += units(a)
    assert plan.n_pointing + 1 + done == len(plan.history), plan.uid
    return out


def write_frames(plan: Plan, frames_dir: Path, image_mode: str):
    d = Path(frames_dir) / plan.uid
    d.mkdir(parents=True, exist_ok=True)
    for p, jpeg in enumerate(plan.history):
        bgr = cv2.cvtColor(navila_view(jpeg, image_mode), cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(d / f"{p:04d}.jpg"), bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])


def stop_repeat_for(n_moves: int, n_episodes: int, target_share: float) -> int:
    """Copies of each stop record that make stops about target_share of the records (at least 1)."""
    if n_episodes == 0:
        return 1
    return max(1, round(target_share * n_moves / ((1 - target_share) * n_episodes)))


def export_sessions(sessions_dir: Path, out_dir: Path, params: dict, target_stop_share: float,
                    eval_fraction: float = 0.15, seed: int = 0, strict: bool = True) -> dict:
    """All kept episodes of all sessions -> out_dir/{train,eval}.json + out_dir/frames/<uid>/NNNN.jpg.

    strict: leave out episodes with any problem (labels or pointing frames can't be trusted);
    otherwise only those that can't be converted at all. Eval episodes (eval_fraction, at least one
    when there are two or more) are held out by episode and get no stop repeats.
    """
    eps = sorted(d for d in Path(sessions_dir).glob("session_*/ep_*") if d.is_dir())
    plans = [plan_episode(d, f"{d.parent.name}_{d.name}", params) for d in eps]
    usable = [p for p in plans if p.actions and p.history and (not strict or not p.problems)]
    moves = sum(len(p.actions) - 1 for p in usable)
    repeat = stop_repeat_for(moves, len(usable), target_stop_share)

    order = sorted(usable, key=lambda p: p.uid)
    random.Random(seed).shuffle(order)
    n_eval = max(1, round(eval_fraction * len(order))) if len(order) >= 2 and eval_fraction > 0 else 0
    splits = {"eval": order[:n_eval], "train": order[n_eval:]}

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for split, ps in splits.items():
        recs = []
        for p in ps:
            write_frames(p, out_dir / "frames", params["image_mode"])
            recs += records(p, repeat if split == "train" else 1)
        with open(out_dir / f"{split}.json", "w") as f:
            json.dump(recs, f)
        counts[split] = {"episodes": len(ps), "records": len(recs)}
    split_of = {p.uid: s for s, ps in splits.items() for p in ps}
    return {
        "episodes found": len(plans), "used": len(usable), "stop repeat (train)": repeat, **counts,
        "per episode": [{"uid": p.uid, "split": split_of.get(p.uid, "left out"), "steps": len(p.actions),
                         "pointing frames": p.n_pointing, "problems": p.problems,
                         "labels": ", ".join(a.text() for a in p.actions)} for p in plans],
    }
