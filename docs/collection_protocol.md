# Collection protocol

How to record pointing episodes. Everything is driven from one terminal on the laptop:
`make collect`. One episode = one bag.

**Raw data only.** An episode holds the instruction you typed, the Orbbec frames, odometry and the
rest of the recorded topics -- nothing is turned into action labels, here or in `make preview`. The
label wording is still being decided ("turn left 15 degrees" vs "I think I should turn left 15
degrees" vs something else), and the odometry in the bag is enough to derive any of them later, so
collecting now does not commit to a format and nothing has to be re-recorded when one is chosen.

## Before a session

**Pi** (on Roxy): nothing to do if it booted, the camera starts by itself. Otherwise `make pi-up`.

**Laptop:**
1. Go2 on and standing. **Close the Unitree app on every phone**: the robot takes one WebRTC client.
2. `STT=false` and `VOICE=false` in `.env`: the instruction is typed, and Roxy's spoken replies are
   not needed for collection. No mic has to be plugged in.
3. `make collect`. It starts bringup (Go2 bridge) if it isn't running, waits for odometry and the
   camera, then shows:
   ```
   [odom 15 Hz | camera 15 fps]  kept 0, discarded 0
   Episode 1: [Enter] start   [q] quit >
   ```
   Lines starting with `!` (`NO ODOM`, `NO CAMERA`) must be fixed first. `make logs` in a second
   terminal shows the Go2 bridge.
4. Optional: `make orbbec-view` in a second terminal, then http://localhost:8090.

## One episode

| Press | What happens |
|---|---|
| **Enter** | `● REC ep_001`: recording starts. Point at the target and type the instruction ("go to that door") |
| **Enter** | Instruction done: it is shown. Drive Roxy to the target with the remote: turn and walk forward, **no strafing, no reversing** |
| **Ctrl-C** | Roxy has stopped at the target: the episode ends |
| **Enter** / **d** / **e** | Keep it / discard it / edit the instruction (typo, or the wrong target) |

Ctrl-C while typing the instruction cancels the episode. An episode with no instruction can't be kept
until one is typed with `e`. `q` (or Ctrl-C at the prompt) ends the session; bringup keeps running so
the Go2 stays connected for the next one (`make down` stops it).

Recording starts at the first Enter, so the pointing gesture made while typing is in the bag. Point
at the target for that whole window: those are the frames that show what "that" meant.

## Where it's saved

On the laptop, in `DATA_DIR` (`.env`, default `./data`, never committed):

```
data/sessions/session_20261012_153000/
  configs/                    copy of configs/ at the start (camera, topics, parameters)
  ep_001/
    ep_001_0.mcap             the bag: Orbbec JPEG frames, camera_info, odom, imu, tf, scan, stt ...
    metadata.yaml
    episode.json              the typed instruction, start / go / end times, message counts, distance
  ep_002/ ...
  discarded/ep_003/           discarded and cancelled episodes, kept just in case
  preview/                    written by make preview
```

Roughly 0.9 MB per second of episode. The topics recorded are `recorder.topics` in
[configs/robots/roxy.yaml](../configs/robots/roxy.yaml). Copy finished sessions to the shared data store
(`data/README.md`).

## Checking what was recorded

```bash
make preview                                   # latest session
make preview SESSION=session_20261012_153000
```

Prints a summary per episode and opens `preview/index.html`: per episode the instruction, a video of
the frames, thumbnails (while the instruction was typed vs while driving), the odometry path from
above, how it was driven (distance, net turn, total turning, sideways, reverse) and every topic in the
bag with its rate and first message. A checklist at the top says whether every kept episode has the
raw data: frames, instruction, odometry, camera intrinsics.

Do a few episodes, `q`, `make preview`, and look before collecting a lot. A single `.mcap` can also be
opened in Foxglove (Open local file) to scrub through every topic.

No labels are produced. Roxy is driven with the Unitree remote, whose commands never pass through
ROS, so whatever the actions turn out to be will be derived from the recorded odometry later.
`data_pipeline/real/path_stats.py` only summarises the driving, to catch what no label format can
repair: strafing and reversing, which a forward/turn action space cannot express. An episode with
those warnings is better re-driven than relabelled.
