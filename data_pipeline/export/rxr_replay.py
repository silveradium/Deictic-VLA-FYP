"""Replay folder from a local copy of NaVILA's RxR: the picked trajectories' records and only their frames.

  make rxr-replay                                      # defaults below
  python -s -m data_pipeline.export.rxr_replay --replay 100 --eval 50 --seed 0

Same selection as mix_navila.ipynb steps 2-3 (pick_trajectories, write_subsets: whole trajectories,
records verbatim, so NaVILA's stop repeats come along), but the frames are copied from the RxR already
on disk instead of streamed from Hugging Face (a 24 GB tar). Run it once, on the machine that has RxR
(the workstation: NAVILA_DIR/NaVILA-Dataset/RxR). Everything goes into one folder that can be rsynced
to the training machine as it is:

  <out>/rxr_replay.json           records of the replay trajectories, mixed into training
  <out>/rxr_eval.json             records of other trajectories, to measure what fine-tuning forgets
                                  (the base model was trained on them: not a generalisation test)
  <out>/rxr/<traj>/frame_N.jpg    only the frames those records use: the image_path of both sets
  <out>/rxr_report.json           settings and counts

The mix step decides how much of the replay set training uses; picking more trajectories here only
costs disk (~3.5 MB each).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from data_pipeline.export.navila_data import (pick_trajectories, scan, stop_share, summary,  # noqa: E402
                                              write_subsets)


def env_path(name: str, default: str) -> Path:
    """A path from the environment or .env (relative paths are relative to the repo root)."""
    value = os.environ.get(name)
    if value is None and (REPO / ".env").exists():
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip()
    p = Path(value or default)
    return p if p.is_absolute() else REPO / p


def safe(rel: str) -> bool:
    pp = PurePosixPath(rel)
    return not pp.is_absolute() and ".." not in pp.parts


def copy_frames(needed, src: Path, dst: Path) -> dict:
    """Copy the needed frames (paths relative to the image folder) from src to dst. Frames already in
    dst are kept, so an interrupted run can be repeated. A frame on disk is always complete."""
    stats = {"needed": len(needed), "already there": 0, "copied": 0, "missing in source": []}
    for rel in sorted(needed):
        if not safe(rel):
            raise ValueError(f"unsafe frame path in the annotations: {rel!r}")
        out = dst / rel
        if out.exists():
            stats["already there"] += 1
            continue
        if not (src / rel).is_file():
            stats["missing in source"].append(rel)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        shutil.copyfile(src / rel, part)
        part.rename(out)
        stats["copied"] += 1
    return stats


def check(records_path: Path, images: Path) -> dict:
    """Every record's frames are on disk under images."""
    recs = json.loads(records_path.read_text())
    frames = {f for r in recs for f in r["frames"]}
    missing = sum(1 for f in frames if not (images / f).is_file())
    return {"records": len(recs), "frames": len(frames), "missing frames": missing}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=env_path("NAVILA_DIR", "NaVila/NaVILA") / "NaVILA-Dataset" / "RxR",
                    help="RxR folder with annotations.json and train/ (default: NAVILA_DIR/NaVILA-Dataset/RxR)")
    ap.add_argument("--out", type=Path, default=env_path("DATA_DIR", "data") / "navila_mix",
                    help="output folder (default: DATA_DIR/navila_mix)")
    ap.add_argument("--replay", type=int, default=100, help="trajectories mixed into training")
    ap.add_argument("--eval", type=int, default=50, help="other trajectories, to measure forgetting")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    annotations, frames_src = args.src / "annotations.json", args.src / "train"
    for p in (annotations, frames_src):
        if not p.exists():
            sys.exit(f"not found: {p} (set --src or NAVILA_DIR)")
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    print(f"scanning {annotations} ({annotations.stat().st_size / 1e9:.1f} GB)", flush=True)
    s = scan("rxr", annotations, progress_every=100_000)
    stats = summary(s)
    if s.unknown:
        sys.exit(f"answers NaVILA's parser does not know: {s.unknown.most_common(5)}")
    print(f"  {stats['records']:,} records, {stats['trajectories']:,} trajectories, "
          f"stop share {stop_share(s):.3f} ({time.time() - t0:.0f} s)", flush=True)

    replay, held_out = pick_trajectories(s, args.replay, args.eval, args.seed)
    del s
    paths = {"replay": out / "rxr_replay.json", "eval": out / "rxr_eval.json"}
    print(f"writing {len(replay)} replay + {len(held_out)} eval trajectories", flush=True)
    res = write_subsets(annotations, {"replay": replay, "eval": held_out}, paths)

    needed = res["replay"]["frames"] | res["eval"]["frames"]
    images = out / "rxr"
    print(f"copying {len(needed):,} frames to {images}", flush=True)
    copied = copy_frames(needed, frames_src, images)
    if copied["missing in source"]:
        print(f"  WARNING {len(copied['missing in source'])} frames not in {frames_src}, "
              f"e.g. {copied['missing in source'][:3]}")

    checks = {g: check(p, images) for g, p in paths.items()}
    size_mb = sum(f.stat().st_size for f in images.rglob("*.jpg")) / 1e6
    ready = all(c["missing frames"] == 0 and c["records"] for c in checks.values())
    report = {
        "source": str(args.src), "seed": args.seed,
        "replay trajectories": replay, "eval trajectories": held_out,
        "rxr stop share": round(stop_share_of(paths["replay"]), 4),
        "copy": {k: (len(v) if isinstance(v, list) else v) for k, v in copied.items()},
        "checks": checks, "frames MB": round(size_mb, 1), "ready": ready,
    }
    (out / "rxr_report.json").write_text(json.dumps(report, indent=1))

    for g, c in checks.items():
        print(f"  rxr_{g:6} {c['records']:6,} records, {c['frames']:6,} frames, missing {c['missing frames']}")
    print(f"  frames on disk: {size_mb:,.0f} MB in {images}")
    print(f"{'READY' if ready else 'NOT READY'} ({time.time() - t0:.0f} s): {out}")
    sys.exit(0 if ready else 1)


def stop_share_of(records_path: Path) -> float:
    from data_pipeline.real.odom_actions import STOP_ANSWER

    recs = json.loads(records_path.read_text())
    return sum(1 for r in recs if r["a"] == STOP_ANSWER) / max(len(recs), 1)


if __name__ == "__main__":
    main()
