"""Look inside a collection session before collecting more: what each episode bag really contains.

  make preview                                  # the latest session in DATA_DIR
  make preview SESSION=session_20261012_153000

Runs in the ROS image (rosbag2_py). For every episode (ep_*/ and discarded/ep_*/) it reads the bag
and episode.json, prints a summary, and writes <session>/preview/:
  index.html     per episode: the instruction, a video of the Orbbec frames, thumbnails (listening /
                 driving), what NaVILA's first decision sees (pointing frames + current frame,
                 data_pipeline/real/navila_input.py), the odometry path from above, the NaVILA
                 action labels derived from it (data_pipeline/real/odom_actions.py), and every topic
                 in the bag with its rate and a sample message, so you can see exactly what is saved.
                 Every image has a dashed box: the part NaVILA keeps (image_mode crop)
  ep_001.mp4     the episode's frames (needs ffmpeg, which the ROS image has)
  ep_001_*.jpg   thumbnails, the recorded JPEGs as they are

Nothing in the session is changed except preview/.
"""

import argparse
import html
import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py import message_to_yaml
from rosidl_runtime_py.utilities import get_message

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from data_pipeline.real.navila_input import crop_box, load_params, pick_gesture  # noqa: E402
from data_pipeline.real.odom_actions import odom_to_actions  # noqa: E402

CAMERA = "/orbbec/color/image_raw/compressed"
ODOM = "/odom"
EVENT = "/episode/event"
N_THUMBS = 8


def yaw_deg(q):
    return math.degrees(math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)))


def read_episode(bag_dir: Path):
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    stamps, samples, frames, odom, events = defaultdict(list), {}, [], [], []
    while reader.has_next():
        topic, data, t_ns = reader.read_next()
        t = t_ns * 1e-9
        stamps[topic].append(t)
        if topic not in samples:
            samples[topic] = data
        if topic == CAMERA:
            frames.append((t, bytes(deserialize_message(data, get_message(types[topic])).data)))
        elif topic == ODOM:
            m = deserialize_message(data, get_message(types[topic]))
            p = m.pose.pose.position
            odom.append((t, p.x, p.y, yaw_deg(m.pose.pose.orientation)))
        elif topic == EVENT:
            events.append(json.loads(deserialize_message(data, get_message(types[topic])).data))
    return types, stamps, samples, frames, odom, events


def jpeg_size(data: bytes):
    """(width, height) from a JPEG's frame header, None if there is none."""
    i = 2
    while i + 9 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xFF or 0xD0 <= marker <= 0xD9 or marker == 0x01:  # fill byte, markers without length
            i += 1 if marker == 0xFF else 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # SOFn: length, precision, h, w
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def sample_yaml(topic, type_name, data, max_lines=18):
    msg = deserialize_message(data, get_message(type_name))
    if topic == CAMERA:
        return f"format: {msg.format}\ndata: <{len(msg.data)} bytes of JPEG>\nheader:\n  frame_id: {msg.header.frame_id}"
    text = message_to_yaml(msg, truncate_length=8)
    lines = text.splitlines()
    return "\n".join(lines[:max_lines] + (["..."] if len(lines) > max_lines else []))


def rate_stats(ts):
    if len(ts) < 2:
        return 0.0, 0.0
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    return (len(ts) - 1) / max(ts[-1] - ts[0], 1e-9), max(gaps)


def write_video(frames, path: Path) -> bool:
    if len(frames) < 2 or not shutil.which("ffmpeg"):
        return False
    fps = max(1.0, (len(frames) - 1) / max(frames[-1][0] - frames[0][0], 1e-9))
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", f"{fps:.2f}",
         "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-movflags", "+faststart", str(path)],
        stdin=subprocess.PIPE)
    for _, jpeg in frames:
        proc.stdin.write(jpeg)
    proc.stdin.close()
    return proc.wait() == 0


def path_svg(odom, size=260, pad=18):
    """Top-down odometry path, rotated so Roxy starts facing up. Green = start, red = end."""
    if len(odom) < 2:
        return '<p class="muted">no odometry</p>'
    _, x0, y0, yaw0 = odom[0]
    a = math.radians(90 - yaw0)  # start heading -> up
    pts = [((x - x0) * math.cos(a) - (y - y0) * math.sin(a), (x - x0) * math.sin(a) + (y - y0) * math.cos(a))
           for _, x, y, _ in odom]
    span = max(0.5, max(max(abs(px), abs(py)) for px, py in pts))
    k = (size / 2 - pad) / span
    xy = [(size / 2 + px * k, size / 2 - py * k) for px, py in pts]
    poly = " ".join(f"{u:.1f},{v:.1f}" for u, v in xy)
    (sx, sy), (ex, ey) = xy[0], xy[-1]
    end_yaw = math.radians(odom[-1][3] - yaw0)  # end heading relative to the start (0 = up)
    ax, ay = ex + 16 * math.sin(-end_yaw), ey - 16 * math.cos(-end_yaw)
    grid = size / 2
    return (f'<svg viewBox="0 0 {size} {size}" class="path" role="img" aria-label="odometry path">'
            f'<line x1="{grid}" y1="0" x2="{grid}" y2="{size}" class="axis"/>'
            f'<line x1="0" y1="{grid}" x2="{size}" y2="{grid}" class="axis"/>'
            f'<text x="4" y="{size - 6}" class="scale">{span:.1f} m to edge</text>'
            f'<polyline points="{poly}" class="trail"/>'
            f'<line x1="{ex:.1f}" y1="{ey:.1f}" x2="{ax:.1f}" y2="{ay:.1f}" class="heading"/>'
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="5" class="start"/>'
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="5" class="end"/></svg>')


def analyse(ep_dir: Path, out: Path, discarded: bool, params: dict):
    name = ep_dir.name
    meta = {}
    if (ep_dir / "episode.json").exists():
        meta = json.loads((ep_dir / "episode.json").read_text())
    if not (ep_dir / "metadata.yaml").exists():
        return {"name": name, "discarded": discarded, "meta": meta,
                "problems": ["bag not closed (no metadata.yaml): ros2 bag reindex <dir> mcap"]}
    types, stamps, samples, frames, odom, events = read_episode(ep_dir)
    t_listen = meta.get("t_listen") or (frames[0][0] if frames else 0)
    t_go = meta.get("t_go") or t_listen
    t_end = meta.get("t_end") or (frames[-1][0] if frames else t_go)

    actions, stats = odom_to_actions([p for p in odom if p[0] >= t_go])
    fps, cam_gap = rate_stats(stamps.get(CAMERA, []))
    odom_hz, odom_gap = rate_stats(stamps.get(ODOM, []))
    problems = list(stats.warnings)
    if not frames:
        problems.append("no camera frames")
    elif cam_gap > 0.5:
        problems.append(f"camera gap {cam_gap:.1f} s")
    if odom and odom_gap > 1.0:
        problems.append(f"odometry gap {odom_gap:.1f} s")
    if not meta.get("instruction"):
        problems.append("no instruction")

    thumbs = []
    if frames:
        idx = sorted({round(i * (len(frames) - 1) / (N_THUMBS - 1)) for i in range(N_THUMBS)})
        for i in idx:
            t, jpeg = frames[i]
            fname = f"{name}_{i:04d}.jpg"
            (out / fname).write_bytes(jpeg)
            thumbs.append((fname, t - t_listen, "listening" if t < t_go else "driving"))
    # what NaVILA's first decision sees: the pointing frames, then the frame at t_go
    first_input, crop = [], None
    if frames:
        size = jpeg_size(frames[0][1])
        crop = crop_box(*size, params["image_mode"]) if size else None
        times = [t for t, _ in frames]
        n = params["gesture_frames"]
        pointing = pick_gesture(times, t_go, params["gesture_window_s"], n)
        current = min(range(len(frames)), key=lambda i: abs(times[i] - t_go))
        for k, i in enumerate(pointing + [current]):
            fname = f"{name}_in{k}_{i:04d}.jpg"
            (out / fname).write_bytes(frames[i][1])
            first_input.append((fname, times[i] - t_listen, "pointing" if k < len(pointing) else "current"))
        if len(pointing) < n:
            problems.append(f"{len(pointing)} of {n} pointing frames: point for {params['gesture_window_s']:.0f} s "
                            "before the second Enter (or a camera gap)")
    video = f"{name}.mp4" if write_video(frames, out / f"{name}.mp4") else None

    topics = []
    for topic in sorted(stamps):
        hz, gap = rate_stats(stamps[topic])
        try:
            sample = sample_yaml(topic, types[topic], samples[topic])
        except Exception as e:  # noqa: BLE001 - a sample is only for display
            sample = f"<could not decode: {e}>"
        topics.append((topic, types[topic], len(stamps[topic]), hz, gap, sample))

    return {"name": name, "discarded": discarded, "meta": meta, "events": events,
            "duration": t_end - t_listen, "listen": t_go - t_listen, "drive": t_end - t_go,
            "frames": len(frames), "fps": fps, "cam_gap": cam_gap, "odom_n": len(odom),
            "odom_hz": odom_hz, "actions": actions, "problems": problems, "thumbs": thumbs,
            "first_input": first_input, "crop": crop, "video": video, "svg": path_svg(odom), "topics": topics}


def print_summary(eps):
    for e in eps:
        m = e["meta"]
        tag = "  DISCARDED" if e["discarded"] else ""
        print(f"\n{e['name']}{tag}  {m.get('instruction', '')!r}")
        if "frames" in e:
            print(f"  {e['duration']:.1f} s ({e['listen']:.1f} listening + {e['drive']:.1f} driving) | "
                  f"{e['frames']} frames, {e['fps']:.1f} fps | odom {e['odom_n']} msgs, {e['odom_hz']:.1f} Hz")
            if e["actions"]:
                print("  labels: " + ", ".join(a.text() for a in e["actions"]))
            n_pointing = sum(1 for *_, kind in e["first_input"] if kind == "pointing")
            print(f"  first decision: {n_pointing} pointing frames + the current one")
            print(f"  topics: {', '.join(t[0] for t in e['topics'])}")
        for p in e["problems"]:
            print(f"  WARNING {p}")


CSS = """
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e2dc; --accent:#2f6fde;
        --ok:#1f8a4c; --bad:#c2410c; --code:#f1f0ec; }
@media (prefers-color-scheme: dark) { :root { --bg:#151514; --card:#1f1f1d; --text:#ecebe6; --muted:#9a9993;
        --line:#33332f; --accent:#7aa7ff; --ok:#4ade80; --bad:#fb923c; --code:#2a2a27; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui, sans-serif } main { max-width:1100px; margin:0 auto; padding:24px 16px 64px }
h1 { font-size:22px; margin:0 0 4px } h2 { font-size:18px; margin:0 } .muted { color:var(--muted) }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; margin:18px 0 }
.head { display:flex; gap:12px; align-items:baseline; flex-wrap:wrap }
.instr { font-size:17px; margin:6px 0 12px } .badge { font-size:12px; padding:2px 8px; border-radius:99px;
  border:1px solid var(--line) } .badge.bad { color:var(--bad); border-color:var(--bad) }
.badge.ok { color:var(--ok); border-color:var(--ok) }
.grid { display:grid; grid-template-columns: minmax(0,1.6fr) minmax(0,1fr); gap:16px }
@media (max-width:760px) { .grid { grid-template-columns:1fr } }
video { width:100%; border-radius:8px; background:#000 }
.facts { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:6px 16px; margin:10px 0 }
.facts div span { display:block; font-size:12px; color:var(--muted) }
.thumbs { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:8px; margin-top:12px }
.thumbs figure { margin:0 } .thumbs img { width:100%; border-radius:6px; display:block }
.thumbs figcaption { font-size:12px; color:var(--muted) } .listening { outline:2px solid var(--accent) }
.view { position:relative } .crop { position:absolute; border:2px dashed var(--bad); pointer-events:none }
.path { width:100%; max-width:260px; background:var(--code); border-radius:8px }
.path .axis { stroke:var(--line) } .path .trail { fill:none; stroke:var(--accent); stroke-width:2.5 }
.path .start { fill:var(--ok) } .path .end { fill:var(--bad) } .path .heading { stroke:var(--bad); stroke-width:2.5 }
.path .scale { fill:var(--muted); font-size:10px }
.labels { font-family:ui-monospace,monospace; font-size:13px; background:var(--code); padding:8px 10px;
  border-radius:6px; margin:8px 0 } .warn { color:var(--bad) }
table { width:100%; border-collapse:collapse; font-size:13px } td, th { text-align:left; padding:4px 8px;
  border-bottom:1px solid var(--line); vertical-align:top } pre { margin:0; font-size:12px; white-space:pre-wrap;
  background:var(--code); padding:6px; border-radius:6px; max-height:240px; overflow:auto }
details { margin-top:12px } summary { cursor:pointer; color:var(--accent) }
.check li { list-style:none } .check { padding-left:0 }
"""


def esc(s):
    return html.escape(str(s))


def figure(fname, caption, cls, crop):
    """A thumbnail with a dashed box over the part NaVILA keeps (none when it keeps the whole frame)."""
    box = ""
    if crop and crop != (0.0, 0.0, 1.0, 1.0):
        left, top, w, h = crop
        box = f'<div class="crop" style="left:{left:.1%};top:{top:.1%};width:{w:.1%};height:{h:.1%}"></div>'
    return (f'<figure><div class="view"><img src="{esc(fname)}" alt="{esc(caption)}" class="{cls}" '
            f'loading="lazy">{box}</div><figcaption>{esc(caption)}</figcaption></figure>')


def episode_html(e):
    m = e["meta"]
    badge = '<span class="badge bad">discarded</span>' if e["discarded"] else '<span class="badge ok">kept</span>'
    if "frames" not in e:
        return (f'<section class="card"><div class="head"><h2>{esc(e["name"])}</h2>{badge}</div>'
                + "".join(f'<p class="warn">{esc(p)}</p>' for p in e["problems"]) + "</section>")
    facts = [("duration", f"{e['duration']:.1f} s"), ("listening", f"{e['listen']:.1f} s"),
             ("driving", f"{e['drive']:.1f} s"), ("camera", f"{e['frames']} frames, {e['fps']:.1f} fps"),
             ("max frame gap", f"{e['cam_gap']:.2f} s"), ("odometry", f"{e['odom_n']} msgs, {e['odom_hz']:.1f} Hz"),
             ("driven", f"{m.get('path_m', 0):.2f} m"), ("turned", f"{m.get('turned_deg', 0):+.0f} deg"),
             ("instruction from", m.get("instruction_source", "?") or "?")]
    heard = "".join(f"<li>{esc(h['text'])}</li>" for h in m.get("heard", []))
    video = (f'<video controls preload="metadata" src="{esc(e["video"])}"></video>' if e["video"]
             else '<p class="muted">no video (ffmpeg missing or under 2 frames)</p>')
    thumbs = "".join(figure(f, f"{t:+.1f} s, {ph}", ph, e["crop"]) for f, t, ph in e["thumbs"])
    first = "".join(figure(f, f"{t:+.1f} s, {kind}", "listening" if kind == "pointing" else "", e["crop"])
                    for f, t, kind in e["first_input"])
    labels = ", ".join(a.text() for a in e["actions"]) or "none (no odometry while driving)"
    rows = "".join(
        f"<tr><td>{esc(t)}<br><span class='muted'>{esc(ty)}</span></td><td>{n}</td><td>{hz:.1f}</td>"
        f"<td>{gap:.2f}</td><td><pre>{esc(sample)}</pre></td></tr>" for t, ty, n, hz, gap, sample in e["topics"])
    warnings = "".join(f'<p class="warn">&#9888; {esc(p)}</p>' for p in e["problems"])
    return f"""
<section class="card" id="{esc(e['name'])}">
  <div class="head"><h2>{esc(e['name'])}</h2>{badge}</div>
  <div class="instr">&ldquo;{esc(m.get('instruction') or '(no instruction)')}&rdquo;</div>
  {warnings}
  <div class="grid">
    <div>{video}</div>
    <div>{e['svg']}<p class="muted" style="font-size:12px">From above, Roxy starts at green facing up;
      red = where it stopped, line = its final heading.</p></div>
  </div>
  <div class="facts">{''.join(f'<div><span>{esc(k)}</span>{esc(v)}</div>' for k, v in facts)}</div>
  <div><span class="muted">NaVILA labels from odometry:</span><div class="labels">{esc(labels)}</div></div>
  {f'<div><span class="muted">STT heard while listening:</span><ul>{heard}</ul></div>' if heard else ''}
  <div><span class="muted">NaVILA's first decision sees these, pointing frames first. The person and where
    they point must be inside the dashed box, the part NaVILA keeps:</span><div class="thumbs">{first}</div></div>
  <div class="thumbs">{thumbs}</div>
  <details><summary>Everything in this bag ({len(e['topics'])} topics)</summary>
    <table><tr><th>topic / type</th><th>msgs</th><th>Hz</th><th>max gap s</th><th>first message</th></tr>{rows}</table>
  </details>
</section>"""


def page_html(session, eps):
    kept = [e for e in eps if not e["discarded"]]
    ok = lambda f: all(f(e) for e in kept) if kept else False  # noqa: E731
    checks = [
        ("RGB frames from the Orbbec in every kept episode", ok(lambda e: e.get("frames", 0) > 0)),
        ("an instruction in every kept episode", ok(lambda e: bool(e["meta"].get("instruction")))),
        ("odometry in every kept episode (the source of the action labels)", ok(lambda e: e.get("odom_n", 0) > 0)),
        ("camera intrinsics (orbbec/color/camera_info)",
         ok(lambda e: any(t[0] == "/orbbec/color/camera_info" for t in e.get("topics", [])))),
        ("no warnings in kept episodes", ok(lambda e: not e["problems"])),
    ]
    check_html = "".join(f"<li>{'&#9989;' if c else '&#10060;'} {esc(t)}</li>" for t, c in checks)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(session)} preview</title>
<style>{CSS}</style></head><body><main>
<h1>{esc(session)}</h1>
<p class="muted">{len(kept)} kept, {len(eps) - len(kept)} discarded. Fine-tuning NaVILA needs, per episode, the RGB
frames, the instruction and the actions; the actions come from odometry.</p>
<ul class="check">{check_html}</ul>
<p class="muted">To scrub a raw bag topic by topic, open its <code>.mcap</code> in Foxglove (Open local file).</p>
{''.join(episode_html(e) for e in eps)}
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", type=Path, help="session folder")
    args = ap.parse_args()
    session = args.session
    ep_dirs = [(d, False) for d in sorted(session.glob("ep_*")) if d.is_dir()]
    ep_dirs += [(d, True) for d in sorted((session / "discarded").glob("ep_*")) if d.is_dir()]
    if not ep_dirs:
        sys.exit(f"{session}: no episodes yet")
    out = session / "preview"
    out.mkdir(exist_ok=True)
    params = load_params()  # the inference settings (configs/robots/roxy.yaml), not the session's copy
    eps = sorted((analyse(d, out, disc, params) for d, disc in ep_dirs), key=lambda e: e["name"])
    print_summary(eps)
    (out / "index.html").write_text(page_html(session.name, eps))
    kept = [e for e in eps if not e["discarded"]]
    usable = sum(1 for e in kept if not e["problems"])
    print(f"\n{usable} of {len(kept)} kept episodes have no warnings")
    print(f"preview: {out / 'index.html'}")


if __name__ == "__main__":
    main()
