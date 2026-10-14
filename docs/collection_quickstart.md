# Data collection quickstart

The short version. Full details: [collection_protocol.md](collection_protocol.md).

## Words

- **Episode**: one recording. Point, say one instruction, drive Roxy to one target. Saved as `ep_001/`, `ep_002/`, ...
- **Session**: one run of `make collect`, from start until you quit. Holds all the episodes from that run.

```
data/sessions/session_20261013_153000/   ← one session
  ep_001/                                ← "go to that door"
  ep_002/                                ← "go to the chair"
```

## Before you start

1. Pi on (the camera starts by itself; if not, `make pi-up`).
2. No mic for now (`STT=false` in `.env`): instructions are typed.
3. Go2 on and standing. **Close the Unitree app on every phone.**
4. First time after pulling new code: `make build-ros`.

## Record

Everything happens in **one terminal**:

```bash
make collect
```

Wait for `Episode 1: [Enter] start`. Fix any line starting with `!` first.

| Step | You do | Key |
|---|---|---|
| 1 | Start recording | **Enter** |
| 2 | Point at the target and type the instruction | |
| 3 | Instruction done | **Enter** |
| 4 | Drive Roxy to the target with the remote (turn on the spot, walk forward, no sideways, no reversing) | |
| 5 | Roxy stopped at the target, so end the episode | **Ctrl-C** |
| 6 | Keep it / discard it / fix the instruction | **Enter** / **d** / **e** |

Back at `Episode 2: [Enter] start`. Repeat from step 1.

## Stop

- **`q`** at the `Episode N` prompt ends the session.
- **`make down`** turns everything off when you're done for the day.

## What Ctrl-C does

| When | Ctrl-C |
|---|---|
| Driving (step 4) | Ends the episode (normal) |
| Typing (step 2) | Cancels the episode |
| At the `Episode N` prompt | Ends the session (same as `q`) |

## Look at what you recorded

```bash
make preview                                  # latest session
make preview SESSION=session_20261013_153000  # a specific one
```

Opens a page per session: instruction, video, path from above, and a checklist of anything missing.
Do 2–3 episodes, `q`, `make preview`, check, *then* record a lot.

## Optional second terminal

Only for watching, never type anything in it while recording:

- `make logs`: what the Go2 connection and the mic are doing (useful when you see a `!` warning).
- `make orbbec-view`, then open http://localhost:8090: live camera.
