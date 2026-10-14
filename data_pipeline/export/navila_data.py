"""NaVILA's released training data (huggingface.co/datasets/a8cheng/NaVILA-Dataset): download, read, check.

Used by mix_navila.ipynb to pick replay data: samples of the sets NaVILA was trained on, mixed into the
deictic fine-tuning set so the model keeps the navigation it learned from them.

R2R, RxR and Human annotations are one JSON list of decision steps:

  {"video_id": "<trajectory>-<step>", "q": instruction, "a": answer, "frames": [history frame paths]}

The answer is the action taken after the last frame. Frame paths are relative to the set's image
folder (R2R/train, RxR/train, Human/raw_frames). NaVILA's loader (llava/data/dataset.py,
LazyVLNCEDataset) builds the prompt around q and samples 8 of the frames: 7 spread over the history
plus the last. Stop steps are repeated in the files to balance the labels.

Frames come as one tar per set (R2R 20 GB, RxR 24 GB) in no useful order: fetch_frames streams it
once and writes only the frames of the picked trajectories.

Pure Python. ijson streams the files: RxR's is 1.5 GB, too big for json.load on a 16 GB laptop.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import tarfile
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import ijson

from data_pipeline.real.odom_actions import (FORWARD, FORWARD_UNIT_M, STOP, TURN_LEFT, TURN_RIGHT,
                                             TURN_UNIT_DEG, Action)

REPO_ID = "a8cheng/NaVILA-Dataset"
ANNOTATIONS = {"r2r": "R2R/annotations.json", "rxr": "RxR/annotations.json", "human": "Human/annotations.json"}
FRAME_TARS = {"r2r": "R2R/train.tar.gz", "rxr": "RxR/train.tar.gz"}  # R2R's is really an uncompressed tar
FRAME_DIRS = {"r2r": "R2R/train", "rxr": "RxR/train"}  # the image_path of the set in datasets_mixture.py

_ANSWER = re.compile(r"The next action is (move forward|turn left|turn right) (\d+) (cm|degree)\.")
_KINDS = {"move forward": FORWARD, "turn left": TURN_LEFT, "turn right": TURN_RIGHT}


def download_annotations(sources, data_dir: Path) -> dict[str, Path]:
    """Fetch the annotation JSONs into data_dir (same layout as the HF repo). Files already there are kept."""
    from huggingface_hub import hf_hub_download

    paths = {}
    for name in sources:
        dest = Path(data_dir) / ANNOTATIONS[name]
        if not dest.exists():  # hf_hub_download writes to a temp file first: an existing file is complete
            hf_hub_download(REPO_ID, ANNOTATIONS[name], repo_type="dataset", local_dir=data_dir)
        paths[name] = dest
    return paths


def iter_steps(path: Path):
    """Records of an annotation file, one at a time."""
    with open(path, "rb") as f:
        yield from ijson.items(f, "item")


def split_video_id(video_id: str) -> tuple[str, int]:
    """"3823-25" -> ("3823", 25). Human trajectory ids contain dashes too ("-23esP--xK8_0-0")."""
    traj, step = str(video_id).rsplit("-", 1)
    return traj, int(step)


def parse_answer(answer: str) -> Action | None:
    """A training answer -> Action (t unused), None if it is not one of NaVILA's 10 answers."""
    if answer == Action(STOP, 0.0, 0.0).answer():
        return Action(STOP, 0.0, 0.0)
    m = _ANSWER.fullmatch(answer)
    if not m:
        return None
    kind = _KINDS[m.group(1)]
    if (m.group(3) == "cm") != (kind == FORWARD):
        return None
    value = int(m.group(2)) / 100 if kind == FORWARD else float(m.group(2))
    return Action(kind, value, 0.0)


def units(action: Action) -> int:
    """25 cm / 15 degree units in an action: one simulator step each."""
    if action.kind == FORWARD:
        return round(action.value / FORWARD_UNIT_M)
    if action.kind in (TURN_LEFT, TURN_RIGHT):
        return round(action.value / TURN_UNIT_DEG)
    return 0


@dataclass
class Scan:
    """One pass over an annotation file: what replay sampling and the checks need."""
    name: str
    records: int = 0
    answers: Counter = field(default_factory=Counter)        # raw answer string -> records
    unknown: Counter = field(default_factory=Counter)        # answers parse_answer rejects
    n_frames: list = field(default_factory=list)             # history length per record
    # trajectory -> {step: (frames, Action, copies)}; copies counts the repeated records
    trajectories: dict = field(default_factory=lambda: defaultdict(dict))
    mismatched_copies: int = 0                               # repeated step ids that differ


def scan(name: str, path: Path, progress_every: int = 0) -> Scan:
    s = Scan(name)
    for rec in iter_steps(path):
        s.records += 1
        a = rec["a"]
        s.answers[a] += 1
        action = parse_answer(a)
        if action is None:
            s.unknown[a] += 1
        frames = rec["frames"]
        s.n_frames.append(len(frames))
        traj, step = split_video_id(rec["video_id"])
        steps = s.trajectories[traj]
        if step in steps:
            prev_frames, prev_action, copies = steps[step]
            if prev_frames != len(frames) or prev_action != action:
                s.mismatched_copies += 1
            steps[step] = (prev_frames, prev_action, copies + 1)
        else:
            steps[step] = (len(frames), action, 1)
        if progress_every and s.records % progress_every == 0:
            print(f"  {name}: {s.records:,} records", flush=True)
    s.trajectories = dict(s.trajectories)
    return s


def summary(s: Scan) -> dict:
    steps = [st for traj in s.trajectories.values() for st in traj.values()]
    kinds = Counter(a.kind for _, a, _ in steps if a is not None)
    stop_copies = Counter(c for _, a, c in steps if a is not None and a.kind == STOP)
    move_copies = Counter(c for _, a, c in steps if a is not None and a.kind != STOP)
    no_stop = sum(1 for traj in s.trajectories.values()
                  if not any(a is not None and a.kind == STOP for _, a, _ in traj.values()))
    per_traj = [len(t) for t in s.trajectories.values()]
    return {
        "records": s.records,
        "trajectories": len(s.trajectories),
        "unique steps": len(steps),
        "steps / trajectory (median)": statistics.median(per_traj) if per_traj else 0,
        "history frames / record (median, max)": (statistics.median(s.n_frames), max(s.n_frames, default=0)),
        "distinct answers": len(s.answers),
        "unknown answers": sum(s.unknown.values()),
        "step share (unique steps)": {k: round(v / max(len(steps), 1), 3) for k, v in kinds.most_common()},
        "stop record copies -> trajectories": dict(sorted(stop_copies.items())),
        "move record copies -> steps": dict(sorted(move_copies.items())),
        "trajectories without a stop": no_stop,
        "repeated step ids that differ": s.mismatched_copies,
    }


def frame_step_check(s: Scan) -> Counter:
    """How the history grows between consecutive steps of a trajectory.

    "per unit": one new frame per 25 cm / 15 degree unit of the action in between (what navila_node
    assumes when it adds frames while moving); "per action": one frame per action. Single-unit actions
    fit both and are counted apart; anything else is counted by its own name.
    """
    out = Counter()
    for traj in s.trajectories.values():
        ordered = sorted(traj.items())
        for (step, (n0, a0, _)), (nxt, (n1, _, _)) in zip(ordered, ordered[1:]):
            if a0 is None:
                out["unknown answer"] += 1
            elif nxt != step + 1:
                out["step ids not consecutive"] += 1
            elif units(a0) == 1 and n1 - n0 == 1:
                out["1-unit action (fits both)"] += 1
            elif n1 - n0 == units(a0):
                out["per unit"] += 1
            elif n1 - n0 == 1:
                out["per action"] += 1
            else:
                out["other"] += 1
    return out


def stop_share(s: Scan) -> float:
    """Share of records that are stops, after NaVILA's repeats: what our export matches."""
    stops = sum(n for a, n in s.answers.items() if a == Action(STOP, 0.0, 0.0).answer())
    return stops / max(s.records, 1)


# ---------------- replay subsets ----------------
def pick_trajectories(s: Scan, n_replay: int, n_eval: int, seed: int) -> tuple[list[str], list[str]]:
    """Random whole trajectories (all their steps and repeats, so NaVILA's label balance comes along):
    n_replay to mix into training, n_eval others to measure forgetting. The base model was trained on
    all of them, so eval measures what fine-tuning loses, not generalisation."""
    ids = sorted(s.trajectories)
    picked = random.Random(seed).sample(ids, min(n_replay + n_eval, len(ids)))
    return sorted(picked[:n_replay]), sorted(picked[n_replay:])


def write_subsets(src: Path, groups: dict[str, list[str]], out: dict[str, Path]) -> dict[str, dict]:
    """One pass over src: the records of each group's trajectories, verbatim, to out[group].
    Returns per group the number of records and the set of frame paths they use."""
    owner = {traj: g for g, trajs in groups.items() for traj in trajs}
    records = {g: [] for g in groups}
    for rec in iter_steps(src):
        g = owner.get(split_video_id(rec["video_id"])[0])
        if g is not None:
            records[g].append(rec)
    result = {}
    for g, recs in records.items():
        out[g].parent.mkdir(parents=True, exist_ok=True)
        with open(out[g], "w") as f:
            json.dump(recs, f)
        result[g] = {"records": len(recs), "frames": {p for r in recs for p in r["frames"]}}
    return result


# ---------------- frames ----------------
class _Counting:
    """File-like wrapper that counts the bytes read through it."""

    def __init__(self, f):
        self.f, self.n = f, 0

    def read(self, size=-1):
        data = self.f.read(size)
        self.n += len(data)
        return data


def fetch_frames(name: str, needed, data_dir: Path, progress_every_s: float = 60.0, timeout_s: float = 120.0,
                 attempts: int = 5) -> dict:
    """Write the needed frames (paths relative to the set's image folder) under data_dir/FRAME_DIRS[name].

    The tar has no index to seek in, so it is streamed from Hugging Face and every member not needed is
    skipped: the whole 20-24 GB crosses the network once, about 1-2 GB reaches the disk. Frames already
    on disk are not fetched again, and reading stops at the last needed frame. A dropped connection is
    retried (attempts) from the start of the tar, keeping what was written; a run that still broke off
    can simply be repeated. Only paths listed in needed are ever written (no path from the tar is trusted).
    """
    from huggingface_hub import hf_hub_url

    dest = Path(data_dir) / FRAME_DIRS[name]
    for p in needed:
        pp = PurePosixPath(p)
        if pp.is_absolute() or ".." in pp.parts:
            raise ValueError(f"unsafe frame path in the annotations: {p!r}")
    todo = {p for p in needed if not (dest / p).exists()}
    stats = {"needed": len(needed), "already there": len(needed) - len(todo), "written": 0, "read GB": 0.0}
    url = hf_hub_url(REPO_ID, FRAME_TARS[name], repo_type="dataset")
    for attempt in range(1, attempts + 1):
        if not todo:
            break
        try:
            _stream_frames(name, url, todo, dest, stats, progress_every_s, timeout_s)
            break
        except (OSError, tarfile.ReadError, EOFError) as e:  # URLError, resets and timeouts are OSErrors
            if attempt == attempts:
                raise
            print(f"  {name}: {e!r}; retry {attempt} of {attempts - 1} in {10 * attempt} s "
                  f"({stats['written']:,} frames kept)", flush=True)
            time.sleep(10 * attempt)
    stats["missing"] = sorted(todo)
    return stats


def _stream_frames(name, url, todo, dest, stats, progress_every_s, timeout_s):
    """One pass over the tar: writes the members in todo, removing them from it."""
    t0 = last = time.time()
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        src = _Counting(resp)
        with tarfile.open(fileobj=src, mode="r|*") as tar:  # stream; plain or gzip, whatever the name says
            for member in tar:
                if member.isfile():
                    rel = member.name[2:] if member.name.startswith("./") else member.name
                    rel = rel.split("/", 1)[1] if rel.startswith("train/") else rel
                    if rel in todo:
                        out = dest / rel
                        out.parent.mkdir(parents=True, exist_ok=True)
                        part = out.with_name(out.name + ".part")
                        part.write_bytes(tar.extractfile(member).read())
                        part.rename(out)  # a frame on disk is always complete
                        todo.discard(rel)
                        stats["written"] += 1
                        if not todo:
                            break
                if time.time() - last > progress_every_s:
                    last = time.time()
                    done = f" of {total / 1e9:.1f}" if total else ""
                    print(f"  {name}: {src.n / 1e9:.1f}{done} GB read, {stats['written']:,} frames written, "
                          f"{len(todo):,} to go, {src.n / 1e6 / (last - t0):.0f} MB/s", flush=True)
    stats["read GB"] = round(stats["read GB"] + src.n / 1e9, 2)
