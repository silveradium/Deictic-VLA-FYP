# Collection protocol

How to record pointing episodes for fine-tuning NaVILA. Everything is driven from one terminal on the
laptop: `make collect`. One episode = one bag.

## Before a session

**Pi** (on Roxy): nothing to do if it booted, the camera starts by itself. Otherwise `make pi-up`.

**Laptop:**
1. Plug the DJI receiver in (transmitter on and linked first). `cat /proc/asound/cards` must list
   `DJI MIC MINI`. STT only finds the mic when bringup starts.
2. Go2 on and standing. **Close the Unitree app on every phone**: the robot takes one WebRTC client.
3. `VOICE=false` in `.env`: Roxy's spoken replies are not needed for collection.
4. `make collect`. It starts bringup (Go2 bridge, mic STT) if it isn't running, waits for odometry and
   the camera, then shows:
   ```
   [odom 15 Hz | camera 15 fps | mic on]  kept 0, discarded 0
   Episode 1: [Enter] start   [q] quit >
   ```
   Lines starting with `!` (`NO ODOM`, `NO CAMERA`, `STT not running`) must be fixed first.
   `make logs` in a second terminal shows the Go2 bridge and STT.
5. Optional: `make orbbec-view` in a second terminal, then http://localhost:8090.

## One episode

| Press | What happens |
|---|---|
| **Enter** | `● REC ep_001 LISTENING`: recording starts. Point at the target and say the instruction ("go to that door"). What the mic hears is printed as `heard: "..."` |
| **Enter** | Done speaking: the instruction is shown. Drive Roxy to the target with the remote: turn on the spot and walk forward, **no strafing, no reversing** |
| **Ctrl-C** | Roxy has stopped at the target: the episode ends |
| **Enter** / **d** / **e** | Keep it / discard it / edit the instruction (STT got it wrong, or someone else spoke) |

Ctrl-C while LISTENING cancels the episode. An episode with no instruction can't be kept until one is
typed with `e`. `q` (or Ctrl-C at the prompt) ends the session; bringup keeps running so the Go2 stays
connected for the next one (`make down` stops it).

Recording starts at the first Enter, so the pointing gesture made while speaking is in the bag. Only
the instruction should be said while listening: everything the mic hears in that window becomes the
instruction.

## Where it's saved

On the laptop, in `DATA_DIR` (`.env`, default `./data`, never committed):

```
data/sessions/session_20261012_153000/
  configs/                    copy of configs/ at the start (camera, topics, parameters)
  ep_001/
    ep_001_0.mcap             the bag: Orbbec JPEG frames, camera_info, odom, imu, tf, scan, stt ...
    metadata.yaml
    episode.json              instruction, what STT heard, listen / go / end times, counts, distance
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
the frames, thumbnails (listening vs driving), the odometry path from above, the NaVILA action labels it
gives (`turn left 45 degrees, move forward 75 cm, ..., stop`) and every topic in the bag with its rate
and first message. A checklist at the top says whether every kept episode has what fine-tuning needs:
frames, instruction, odometry, camera intrinsics.

Do a few episodes, `q`, `make preview`, and look before collecting a lot. A single `.mcap` can also be
opened in Foxglove (Open local file) to scrub through every topic.

Labels are computed from odometry, not recorded: Roxy is driven with the Unitree remote, whose commands
never pass through ROS. `data_pipeline/real/odom_actions.py` rounds the path to NaVILA's units
(15 degree turns, 25 cm steps, at most 45 degrees / 75 cm per action).
