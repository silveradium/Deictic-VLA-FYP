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
3. `make collect`. It starts bringup (Go2 bridge) if it isn't running, asks for the session name,
   waits for odometry and the camera, then shows:
   ```
   Session name (e.g. S1-R1) > S1-R1
     new session

   [odom 15 Hz | camera 15 fps]  0 episode(s) recorded this run
   Next episode (ep_001): [Enter] start   [q] quit >
   ```
   The session name is the folder the episodes go in: a new name makes it, a name already in
   `DATA_DIR/sessions` adds to it (so a session can be continued after quitting, or after a crash).
   Lines starting with `!` (`NO ODOM`, `NO CAMERA`) must be fixed first. `make logs` in a second
   terminal shows the Go2 bridge.
4. Optional: `make orbbec-view` in a second terminal, then http://localhost:8090.

## One episode

| Press | What happens |
|---|---|
| **Enter** | `● REC`: recording starts. Point at the target, then drive Roxy to it with the remote: turn and walk forward, **no strafing, no reversing** |
| **Enter** | Roxy has stopped at the target: recording ends |
| type + **Enter** | The instruction ("go to that door"). Empty is asked twice, then allowed |
| type + **Enter** | The episode name. Enter takes the offered `ep_NNN`; a name that already exists in the session overwrites that episode |

Everything between the two Enters is in the bag, pointing gesture included. Point at the target at
the start of the recording and keep driving afterwards: those first frames are the ones that show
what "that" meant. The instruction and the name are typed after the recording, so taking a moment
over them costs nothing.

Nothing is discarded: to drop an episode, delete its folder; to redo one, record it again and give
it the same name. `q` (or Ctrl-C at the prompt) ends the session; bringup keeps running so the Go2
stays connected for the next one (`make down` stops it). Ctrl-C while recording ends the recording,
like Enter.

## Where it's saved

On the laptop, in `DATA_DIR` (`.env`, default `./data`, never committed):

```
data/sessions/S1-R1/
  configs/                    copy of configs/ at the start (camera, topics, parameters)
  ep_001/
    ep_001_0.mcap             the bag: Orbbec JPEG frames, camera_info, odom, imu, tf, scan, stt ...
    metadata.yaml
    episode.json              the typed instruction, start / end times, message counts, distance
  ep_002/ ...
  door-again/                 whatever you named it at the prompt
  preview/                    written by make preview
```

While an episode is recording its bag is `_recording/`; it is renamed to the name you type. A
`_recording/` left behind by a crash is replaced by the next episode, so copy it out first if you
want it (`ros2 bag reindex _recording mcap` to close it).

Roughly 0.9 MB per second of episode. The topics recorded are `recorder.topics` in
[configs/robots/roxy.yaml](../configs/robots/roxy.yaml). Copy finished sessions to the shared data store
(`data/README.md`).

## Checking what was recorded

```bash
make preview                 # the session recorded last
make preview SESSION=S1-R1
```

Prints a summary per episode and opens `preview/index.html`: per episode the instruction, a video of
the frames, thumbnails, the odometry path from above, how it was driven (distance, net turn, total
turning, sideways, reverse) and every topic in the bag with its rate and first message. A checklist
at the top says whether every episode has the raw data: frames, instruction, odometry, camera
intrinsics.

Do a few episodes, `q`, `make preview`, and look before collecting a lot. A single `.mcap` can also be
opened in Foxglove (Open local file) to scrub through every topic.

No labels are produced. Roxy is driven with the Unitree remote, whose commands never pass through
ROS, so whatever the actions turn out to be will be derived from the recorded odometry later.
`data_pipeline/real/path_stats.py` only summarises the driving, to catch what no label format can
repair: strafing and reversing, which a forward/turn action space cannot express. An episode with
those warnings is better re-driven than relabelled.
