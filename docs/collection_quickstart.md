# Data collection quickstart

The short version. Full details: [collection_protocol.md](collection_protocol.md).

## Words

- **Episode**: one recording. Point, type one instruction, drive Roxy to one target. You name it
  (`ep_001/` is offered); the same name again overwrites that episode.
- **Session**: a folder of episodes. You name it too (`S1-R1`) when `make collect` starts: a new name
  makes a new folder, a name you used before adds episodes to it.

```
data/sessions/S1-R1/   ← one session, named at the start
  ep_001/              ← "go to that door"
  ep_002/              ← "go to the chair"
```

## Before you start

1. Pi on (the camera starts by itself; if not, `make pi-up`).
2. No mic (`STT=false` in `.env`): instructions are typed.
3. Go2 on and standing. **Close the Unitree app on every phone.**
4. First time after pulling new code: `make build-ros`.

## Record

Everything happens in **one terminal**:

```bash
make collect
```

It asks for the session name first (`S1-R1`), then waits for odometry and the camera. Fix any line
starting with `!` before recording.

| Step | You do | Key |
|---|---|---|
| 1 | Start recording | **Enter** |
| 2 | Point at the target, then drive Roxy to it with the remote (turn on the spot, walk forward, no sideways, no reversing) | |
| 3 | Roxy stopped at the target, so end the recording | **Enter** |
| 4 | Type the instruction ("go to that door") | **Enter** |
| 5 | Type the episode name, or take the offered `ep_001` | **Enter** |

Back at `Next episode (ep_002): [Enter] start`. Repeat from step 1.

Nothing is discarded for you: to drop an episode, delete its folder, or record it again under the
same name.

## Stop

- **`q`** at the `Next episode` prompt ends the session.
- **`make down`** turns everything off when you're done for the day.

## What Ctrl-C does

| When | Ctrl-C |
|---|---|
| Recording (step 2) | Ends the recording, same as Enter |
| At the `Next episode` prompt | Ends the session (same as `q`) |

## Look at what you recorded

```bash
make preview                 # the session recorded last
make preview SESSION=S1-R1   # a specific one
```

Opens a page per session: instruction, video, path from above, how it was driven, and a checklist of
anything missing. Do 2–3 episodes, `q`, `make preview`, check, *then* record a lot.

Only raw data is saved -- instruction, frames, odometry, the rest of the topics. No action labels:
the wording is still open, and odometry in the bag is enough to derive them later.

## Optional second terminal

Only for watching, never type anything in it while recording:

- `make logs`: what the Go2 connection is doing (useful when you see a `!` warning).
- `make orbbec-view`, then open http://localhost:8090: live camera.
