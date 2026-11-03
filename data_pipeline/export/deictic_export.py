"""Our collected episodes -> NaVILA's training format (the same as R2R / RxR in a8cheng/NaVILA-Dataset).

  make export SESSION=S1-R1
  python -s -m data_pipeline.export.deictic_export data/S1-R1 [more sessions] [--eval-fraction 0.2]

Per kept episode (<session>/ep_*/: bag + episode.json from `make collect`), one record per decision,
NaVILA's answer for it, and the history it was taken with:

  {"video_id": "<session>_<ep>-<step>", "q": instruction, "a": answer, "frames": ["<uid>/0000.jpg", ...]}

The history is what navila_node gives NaVILA at inference, built by the same rules
(data_pipeline/real/navila_input.py, values from configs/robots/roxy.yaml):
  - the pointing frames from just before t_go (when the instruction was given / driving starts),
  - the frame at t_go,
  - then one frame per 25 cm / 15 degree unit, taken when odometry says the unit was completed
    (as in NaVILA's data, checked in mix_navila.ipynb step 1).
Frames are squared like navila_node.to_navila_view (OpenCV, image_mode, 512 px).

Odometry arrives seconds later than the camera frames (data_pipeline/real/sync.py): it is moved onto
the camera's clock first, per episode, or every frame would show the robot later than its label says.
The same lag means the recording can end before the odometry has seen the robot stop. Such an episode
keeps only the actions the odometry saw completed (its last one may have gone on, so it is dropped too)
and gets no stop record.

t_go is the second Enter where the recorder logged it. Recorders that only log start and end (the
instruction typed afterwards) have no such time: there t_go is just before the robot starts moving,
which is when the pointing ends.

Stop records are repeated so stops make up the same share of the records as in the replay data.
NaVILA repeats each stop 3x (R2R) or 5x (RxR), but its trajectories are 25-40 steps long; ours are a
few steps, so the same repeat would make stop several times more frequent than NaVILA ever saw.

The eval split is random, drawn separately within each group of first turn (left / right / straight)
x size (under / over 45 degrees), so the held-out episodes have their share of each.

Each train episode is also written mirrored (uid suffix _flip): frames flipped left-right, turn left and
turn right swapped, "left" / "right" swapped in the instruction. A mirrored episode is a valid one: the
person points to the other side and the robot turns that way. This works because the frames are cropped
about the image centre (the robot's heading). It doubles the train set and balances left and right
turns, but adds no new rooms, people or targets. Eval episodes are never mirrored.

Runs without ROS: rosbags reads the mcap bags. Writes <out>/{train,eval}.json, <out>/frames/<uid>/
and <out>/export_report.json.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np

from data_pipeline.export.navila_data import units
from data_pipeline.real.navila_input import MAX_GAP_S, load_params, pick_gesture
from data_pipeline.real.odom_actions import (STOP, TURN_LEFT, TURN_RIGHT, Action, merge_units, odom_to_units,
                                             wrap_deg)
from data_pipeline.real.path_stats import path_stats
from data_pipeline.real.sync import MIN_CORR, estimate_lag, to_camera_clock

CAMERA = "/orbbec/color/image_raw/compressed"
ODOM = "/odom"
VIEW_SIZE = 512  # navila_node.to_navila_view

MOVE_M, TURN_DEG = 0.05, 4.0  # leaving the standing pose by more than this = driving has started
GO_MARGIN_S = 0.2             # t_go this long before that, so the frame at t_go is still standing
STILL_WINDOW_S = 0.6          # the odometry's last this-many seconds decide whether the stop was seen
STILL_LIN, STILL_ANG = 0.08, 8.0  # m/s, deg/s: slower than this counts as stopped
ODOM_GAP_S = 1.0              # longer gaps while driving: labels can't be trusted
MIN_POINTING = 2              # fewer pointing frames than this: the gesture is not in the history
FLIP_SUFFIX = "_flip"


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


def motion_onset(odom, t_from: float):
    """(time the robot leaves the pose it stood in at t_from, that pose); (None, pose) if it never does."""
    start = next((p for p in odom if p[0] >= t_from), None)
    if start is None:
        return None, None
    for t, x, y, a in odom:
        if t > start[0] and (math.hypot(x - start[1], y - start[2]) > MOVE_M
                             or abs(wrap_deg(a - start[3])) > TURN_DEG):
            return t, start
    return None, start


def stopped_at_end(odom) -> bool:
    """Whether the odometry's last STILL_WINDOW_S seconds show the robot standing still."""
    t_end = odom[-1][0]
    tail = [p for p in odom if p[0] >= t_end - STILL_WINDOW_S] or odom[-3:]
    if len(tail) < 2:
        return False
    dt = max(tail[-1][0] - tail[0][0], 1e-3)
    lin = math.hypot(tail[-1][1] - tail[0][1], tail[-1][2] - tail[0][2]) / dt
    ang = abs(wrap_deg(tail[-1][3] - tail[0][3])) / dt
    return lin < STILL_LIN and ang < STILL_ANG


def leading_turn(actions) -> float:
    """The turn before the first forward step, degrees, + left / - right."""
    deg = 0.0
    for a in actions:
        if a.kind == TURN_LEFT:
            deg += a.value
        elif a.kind == TURN_RIGHT:
            deg -= a.value
        else:
            break
    return deg


def instruction_tags(text: str) -> dict:
    """What the wording says, for the eval breakdown: the verb, and whether an object is named."""
    words = re.findall(r"[a-z]+", text.lower())
    return {"verb": words[0] if words else "", "named": not ({"here", "there"} & set(words))}


@dataclass
class Plan:
    """One episode, before anything is written."""
    uid: str
    instruction: str = ""
    actions: list = field(default_factory=list)   # merged actions, stop last when the stop was recorded
    history: list = field(default_factory=list)   # JPEG bytes, in history order
    n_pointing: int = 0
    complete: bool = False                        # the odometry saw the robot stop
    lag: float | None = None                      # odometry behind the camera, s
    lag_corr: float = 0.0
    t_go: float | None = None                     # s after the first frame
    first_turn_deg: float = 0.0                   # leading turn, + left / - right
    problems: list = field(default_factory=list)
    flipped: bool = False                         # frames written mirrored left-right


def plan_episode(ep_dir: Path, uid: str, params: dict) -> Plan:
    plan = Plan(uid)
    meta = json.loads((ep_dir / "episode.json").read_text()) if (ep_dir / "episode.json").exists() else {}
    plan.instruction = " ".join((meta.get("instruction") or "").split())
    if not meta:
        plan.problems.append("no episode.json")
    if meta.get("kept") is False:  # recorders without a keep flag delete discarded episodes instead
        plan.problems.append("discarded (kept: false)")
    if not plan.instruction:
        plan.problems.append("no instruction")
    if not (ep_dir / "metadata.yaml").exists():
        plan.problems.append("bag not closed (no metadata.yaml): ros2 bag reindex <dir> mcap")
    if plan.problems:
        return plan

    frames, odom = read_bag(ep_dir)
    if len(frames) < 2:
        plan.problems.append("no camera frames")
    if len(odom) < 10:
        plan.problems.append("no odometry")
    if plan.problems:
        return plan

    plan.lag, plan.lag_corr = estimate_lag(frames, odom)
    if plan.lag is None or plan.lag_corr < MIN_CORR:
        plan.problems.append(f"camera-odometry lag not measurable (best correlation {plan.lag_corr:.2f})")
        return plan
    odom = to_camera_clock(odom, plan.lag)
    times = [t for t, _ in frames]

    if meta.get("t_go"):  # the second Enter, on the recorder's (= camera's) clock
        t_go = meta["t_go"]
        driving = [p for p in odom if p[0] >= t_go]
    else:
        onset, start = motion_onset(odom, times[0])
        if onset is None:
            plan.problems.append("the robot never moves")
            return plan
        t_go = max(times[0], onset - GO_MARGIN_S)
        driving = [(t_go, *start[1:])] + [p for p in odom if p[0] >= onset]  # from the standing pose
    plan.t_go = round(t_go - times[0], 2)
    if len(driving) < 2:
        plan.problems.append("no odometry while driving")
        return plan
    gap = max(b[0] - a[0] for a, b in zip(driving, driving[1:]))
    if gap > ODOM_GAP_S:
        plan.problems.append(f"odometry gap {gap:.1f} s while driving")
    plan.problems += path_stats(driving).warnings

    unit_actions, _ = odom_to_units(driving)
    actions = merge_units(unit_actions)
    plan.first_turn_deg = leading_turn(actions)
    plan.complete = stopped_at_end(odom)
    if plan.complete:
        actions.append(Action(STOP, 0.0, driving[-1][0]))
    else:
        actions = actions[:-1]  # it may have gone on after the odometry ends
    if not actions:
        plan.problems.append("no action completed before the odometry ends")
        return plan
    plan.actions = actions
    n_units = sum(units(a) for a in actions)

    pointing = pick_gesture(times, t_go, params["gesture_window_s"], params["gesture_frames"])
    plan.n_pointing = len(pointing)
    if len(pointing) < MIN_POINTING:
        plan.problems.append(f"{len(pointing)} pointing frames (driving starts {plan.t_go:.1f} s in)")
    observed = []
    for t in [t_go] + [u.t for u in unit_actions[:n_units]]:
        i = nearest(times, t)
        if abs(times[i] - t) > MAX_GAP_S:
            plan.problems.append(f"no frame within {MAX_GAP_S} s of {t - t_go:+.1f} s after go")
        observed.append(i)
    plan.history = [frames[i][1] for i in pointing + observed]
    return plan


def swap_left_right(text: str) -> str:
    sides = {"left": "right", "right": "left", "Left": "Right", "Right": "Left", "LEFT": "RIGHT", "RIGHT": "LEFT"}
    return re.sub(r"\b(left|right)\b", lambda m: sides.get(m.group(0), m.group(0)), text, flags=re.IGNORECASE)


def mirrored(plan: Plan) -> Plan:
    """The episode mirrored left-right: same frames written flipped, turns and wording swapped."""
    other = {TURN_LEFT: TURN_RIGHT, TURN_RIGHT: TURN_LEFT}
    return replace(plan, uid=plan.uid + FLIP_SUFFIX, instruction=swap_left_right(plan.instruction),
                   actions=[replace(a, kind=other.get(a.kind, a.kind)) for a in plan.actions],
                   first_turn_deg=-plan.first_turn_deg, flipped=not plan.flipped)


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
        if plan.flipped:
            bgr = cv2.flip(bgr, 1)
        cv2.imwrite(str(d / f"{p:04d}.jpg"), bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])


def stop_repeat_for(n_moves: int, n_stops: int, target_share: float) -> int:
    """Copies of each stop record that make stops about target_share of the records (at least 1)."""
    if n_stops == 0:
        return 1
    return max(1, round(target_share * n_moves / ((1 - target_share) * n_stops)))


def turn_group(plan: Plan) -> str:
    deg = plan.first_turn_deg
    if abs(deg) < 7.5:
        return "straight"
    return ("left" if deg > 0 else "right") + ("_large" if abs(deg) >= 45 else "_small")


def split_by_turn(plans: list[Plan], eval_fraction: float, seed: int) -> dict[str, list[Plan]]:
    """Random split, drawn separately within each turn_group, so eval gets its share of each."""
    groups = defaultdict(list)
    for p in sorted(plans, key=lambda p: p.uid):
        groups[turn_group(p)].append(p)
    rng = random.Random(seed)
    splits = {"eval": [], "train": []}
    for key in sorted(groups):
        g = groups[key]
        rng.shuffle(g)
        n = round(eval_fraction * len(g))
        splits["eval"] += g[:n]
        splits["train"] += g[n:]
    return splits


def export_episodes(ep_dirs, out_dir: Path, params: dict, target_stop_share: float,
                    eval_fraction: float = 0.2, seed: int = 0, strict: bool = True, flip: bool = True) -> dict:
    """Episodes -> out_dir/{train,eval}.json + out_dir/frames/<uid>/NNNN.jpg (overwritten; frames of
    episodes left out since an earlier run stay on disk, unreferenced).

    strict: leave out episodes with any problem (labels or pointing frames can't be trusted);
    otherwise only those that can't be converted at all. Eval episodes get no stop repeats.
    flip: add each train episode mirrored as well (after the split, so eval stays unseen).
    """
    plans = [plan_episode(d, f"{d.parent.name}_{d.name}", params) for d in ep_dirs]
    usable = [p for p in plans if p.actions and p.history and (not strict or not p.problems)]
    splits = split_by_turn(usable, eval_fraction, seed)
    split_of = {p.uid: s for s, ps in splits.items() for p in ps}
    if flip:
        splits["train"] = splits["train"] + [mirrored(p) for p in splits["train"]]
    train = splits["train"]
    moves = sum(1 for p in train for a in p.actions if a.kind != STOP)
    repeat = stop_repeat_for(moves, sum(p.complete for p in train), target_stop_share)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for split, ps in splits.items():
        recs = []
        for p in sorted(ps, key=lambda p: p.uid):
            write_frames(p, out_dir / "frames", params["image_mode"])
            recs += records(p, repeat if split == "train" else 1)
        with open(out_dir / f"{split}.json", "w") as f:
            json.dump(recs, f)
        counts[split] = {"episodes": len(ps), "mirrored": sum(p.flipped for p in ps),
                         "with stop": sum(p.complete for p in ps), "records": len(recs),
                         "turn groups": dict(sorted(Counter(turn_group(p) for p in ps).items())),
                         "answers": dict(Counter(r["a"] for r in recs).most_common())}
    return {
        "episodes found": len(plans), "used": len(usable), "stop repeat (train)": repeat,
        "mirrored train episodes": flip,
        "target stop share": target_stop_share, "eval fraction": eval_fraction, "seed": seed,
        "inference settings": params, **counts,
        "per episode": [{"uid": p.uid, "split": split_of.get(p.uid, "left out"), "instruction": p.instruction,
                         **instruction_tags(p.instruction), "turn group": turn_group(p) if p.actions else "",
                         "first turn deg": p.first_turn_deg, "steps": len(p.actions), "stop seen": p.complete,
                         "pointing frames": p.n_pointing, "lag s": p.lag, "lag corr": p.lag_corr,
                         "t_go s": p.t_go, "problems": p.problems,
                         "labels": ", ".join(a.text() for a in p.actions)} for p in plans],
    }


def export_sessions(sessions_dir: Path, out_dir: Path, params: dict, target_stop_share: float,
                    eval_fraction: float = 0.15, seed: int = 0, strict: bool = True, flip: bool = True) -> dict:
    """All episodes of all sessions under sessions_dir (<session>/ep_*), as export_episodes."""
    eps = sorted(d for d in Path(sessions_dir).glob("*/ep_*") if d.is_dir())
    return export_episodes(eps, out_dir, params, target_stop_share, eval_fraction, seed, strict, flip)


def main():
    from data_pipeline.export.rxr_replay import env_path

    mix_dir = env_path("DATA_DIR", "data") / "navila_mix"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+", type=Path, help="session folders, each with ep_*/ episode folders")
    ap.add_argument("--out", type=Path, default=mix_dir / "deictic", help="default: DATA_DIR/navila_mix/deictic")
    ap.add_argument("--eval-fraction", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--stop-share", type=float, default=None,
                    help="share of stop records; default: the RxR replay set's (navila_mix/rxr_report.json), else 0.08")
    ap.add_argument("--no-strict", action="store_true", help="keep episodes with warnings")
    ap.add_argument("--no-flip", action="store_true", help="don't add mirrored copies of the train episodes")
    args = ap.parse_args()

    share = args.stop_share
    if share is None:
        report = mix_dir / "rxr_report.json"
        share = json.loads(report.read_text()).get("rxr stop share", 0.08) if report.exists() else 0.08
    eps = []
    for s in args.sessions:
        found = sorted(d for d in s.glob("ep_*") if d.is_dir())
        if not found:
            sys.exit(f"no ep_* folders in {s}")
        eps += found
    params = load_params()
    print(f"{len(eps)} episodes, inference settings {params}, stop share {share:.3f}", flush=True)
    rep = export_episodes(eps, args.out, params, share, args.eval_fraction, args.seed, not args.no_strict,
                          not args.no_flip)
    (args.out / "export_report.json").write_text(json.dumps(rep, indent=1))

    left_out = Counter()
    for e in rep["per episode"]:
        if e["split"] == "left out":
            left_out[re.sub(r"[\d.]+", "N", e["problems"][0]) if e["problems"] else "no actions"] += 1
    print(f"used {rep['used']} of {rep['episodes found']}; stop repeat {rep['stop repeat (train)']}")
    for reason, n in left_out.most_common():
        print(f"  left out {n:3}: {reason}")
    for split in ("train", "eval"):
        c = rep[split]
        print(f"{split:5}: {c['episodes']} episodes ({c['mirrored']} mirrored, {c['with stop']} with stop), "
              f"{c['records']} records, "
              f"turn groups {c['turn groups']}")
    print("instructions:", dict(Counter(e["instruction"] for e in rep["per episode"]).most_common()))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
