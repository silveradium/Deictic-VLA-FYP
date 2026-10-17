"""Which camera view makes NaVILA go forward? Asks the server about one live frame, many ways.

  make navila-probe            robot standing still, facing open space (a corridor, a door ~3 m away)

For every view (Orbbec crop / squash / pad, Go2 front camera squash for comparison) and every probe
instruction, it sends NaVILA exactly what navila_node sends at the start of an episode (the current
frame, padded to the model's frame count) and prints what it answers. Nothing moves.

Same code path as the robot: to_navila_view / image_to_rgb from navila_node, encode_jpeg / sample_frames
from the NaVILA fork's client. Results and the frames sent go to data/probe/<time>/.
"""

import argparse
import json
import os
import time
from datetime import datetime

import cv2
import numpy as np
import rclpy
import requests
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage, Image

from navila_go2.common.client import encode_jpeg, sample_frames
from navila_go2.common.mid_level import parse_navila_output
from navila_node.navila_node import image_to_rgb, to_navila_view

INSTRUCTIONS = [
    "Walk forward and stop.",
    "Walk forward to the door and stop.",
    "Turn left and stop.",
    "Stop.",
]
VIEWS = [  # (name, camera, image_mode)
    ("orbbec crop", "orbbec", "crop"),
    ("orbbec squash", "orbbec", "squash"),
    ("orbbec pad", "orbbec", "pad"),
    ("go2 squash", "go2", "squash"),
]


def grab_frames(timeout=10.0):
    """One RGB frame from each camera (None for a camera that isn't publishing)."""
    node = rclpy.create_node("navila_probe")
    got = {}

    def orbbec(msg):
        got.setdefault("orbbec", cv2.cvtColor(cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR),
                                              cv2.COLOR_BGR2RGB))

    node.create_subscription(CompressedImage, "orbbec/color/image_raw/compressed", orbbec, qos_profile_sensor_data)
    node.create_subscription(Image, "camera/image_raw", lambda m: got.setdefault("go2", image_to_rgb(m)),
                             qos_profile_sensor_data)
    t0 = time.time()
    while len(got) < 2 and time.time() - t0 < timeout:
        rclpy.spin_once(node, timeout_sec=0.2)
    node.destroy_node()
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("NAVILA_SERVER_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--instruction", action="append", help="probe instruction (repeatable; default: a fixed set)")
    ap.add_argument("--out", default="/deictic/data/probe")
    args = ap.parse_args()
    instructions = args.instruction or INSTRUCTIONS

    health = requests.get(f"{args.server}/health", timeout=5).json()
    num_frames = int(health.get("num_frames", 8))
    print(f"NaVILA {health.get('model')} at {args.server}, {num_frames} frames per request")

    rclpy.init()
    frames = grab_frames()
    rclpy.shutdown()
    if not frames:
        raise SystemExit("no camera frames: is the Pi camera up and the Go2 connected?")
    missing = {cam for _, cam, _ in VIEWS} - set(frames)
    if missing:
        print(f"no frames from: {', '.join(sorted(missing))} (skipped)")

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for name, cam, mode in VIEWS:
        if cam not in frames:
            continue
        view = to_navila_view(frames[cam], mode)
        cv2.imwrite(os.path.join(out_dir, name.replace(" ", "_") + ".jpg"), cv2.cvtColor(view, cv2.COLOR_RGB2BGR))
        # episode start, as navila_node: one frame of history, padded to num_frames
        payload_frames = [encode_jpeg(f) for f in sample_frames([view], num_frames)]
        for instruction in instructions:
            r = requests.post(f"{args.server}/act", json={"instruction": instruction, "frames": payload_frames},
                              timeout=120)
            r.raise_for_status()
            text = r.json()["text"]
            action = parse_navila_output(text)
            results.append({"view": name, "instruction": instruction, "text": text, "action": str(action),
                            "kind": action.kind})
            print(f"  {name:<14} | {instruction:<36} -> {action}")

    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump({"server": args.server, "health": health, "results": results}, f, indent=2)

    # summary: which views go forward when asked to, and still turn when asked to turn
    print("\n=== forward instructions answered with forward ===")
    forward_qs = [q for q in instructions if "forward" in q.lower()]
    good = []
    for name, _, _ in VIEWS:
        rows = [r for r in results if r["view"] == name]
        if not rows:
            continue
        fwd = sum(r["kind"] == "forward" for r in rows if r["instruction"] in forward_qs)
        turns_ok = all(r["kind"].startswith("turn") for r in rows if r["instruction"].lower().startswith("turn"))
        print(f"  {name:<14} {fwd}/{len(forward_qs)} forward{'' if turns_ok else '  (turn instruction not turning)'}")
        if forward_qs and fwd == len(forward_qs) and turns_ok:
            good.append(name)
    print(f"\n{'Use: ' + ', '.join(good) if good else 'No view goes forward reliably from here.'}"
          f"  (frames and results in {out_dir.replace('/deictic/', '')})")


if __name__ == "__main__":
    main()
