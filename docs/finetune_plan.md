# Fine-tuning NaVILA on our pointing episodes

Plan and current state, so work can continue on any machine or in a new session. Branch:
`data/deictic-finetune`. The question it answers: does LoRA fine-tuning on our real Go2 episodes
teach NaVILA to go where a person points, and by how much compared with the base model?

## What is on this branch

- Pulitha's `data/navila-mix`: `data_pipeline/export/` (bag -> NaVILA format, replay mixing,
  `mix_navila.ipynb`), `data_pipeline/real/navila_input.py` (crop and pointing-frame rules shared with
  inference), `navila_node` putting the pointing frames at the front of the history.
- `feat/data-pipeline` merged in: the raw-only recorder (instruction typed, no labels at collection)
  and `data_pipeline/real/path_stats.py` (driving summary in `make preview`).
- `data_pipeline/real/odom_actions.py` is kept: the export uses it to turn odometry into labels.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Labels | NaVILA's exact training wording, made from odometry at export time: `The next action is turn left 45 degree.`, `... move forward 75 cm.`, `I think I should stop because I have finished the instruction.` | 130 episodes is too few to also teach a new output format; keeps the base model comparable |
| Model input | Same rules as inference (`roxy.yaml` navila_node: `image_mode`, `gesture_frames`, `gesture_window_s`). Pointing frames go first in the history; NaVILA's frame sampling always keeps frame 0 | What the model is trained on must match what Roxy gives it |
| Training | LoRA on the language model, 4-bit base, vision tower and projector frozen | Small dataset; fits a 24 GB card |
| Hardware | ENTC server, 2x 24 GB GPUs, data parallel (`torchrun --nproc_per_node=2`) | Each GPU holds its own 4-bit copy |
| Replay | RxR only: per epoch our 666 train records x2 plus ~2,000 RxR records (29 whole trajectories from `rxr_replay.json`), so ours are ~40% | Avoid forgetting general navigation without repeating our small set 10x+; R2R would be a 20 GB download |
| Test split | Random 20% of episodes, drawn within each first-turn group (left / right x under / over 45 degrees, straight) | Recording order did not always follow the protocol, so spots can't be recovered; the groups keep left/right and small/large turns balanced in the test set |
| Missing endings | Keep only actions the odometry saw completed; no stop record when the stop was not seen | The odometry lag cut off the end of most drives (below); invented endings would be worse than fewer labels |
| Mirroring | Every train episode is added a second time mirrored: frames flipped left-right, turn left / right swapped (`--no-flip` to leave out). Eval episodes are never mirrored | A mirrored episode is a valid one (person points the other way, robot turns the other way; frames are cropped about the heading). Doubles the train set and balances left / right, but adds no new rooms, people or targets |

## The dataset (Session 1)

One room (HQ living room), one person, 130 episodes, fixed wording ("go there", "go to the bin").
T0 empty floor (10 episodes, P1-P10). T1 dustbin, T2 chair, T3 shoes, T4 fan, T5 brooms with
distractors, T6 doors (20 each). Blocks of 5: A in view + gesture, B in view + gesture + name (spots
P1'-P5'), C out of view + gesture, D out of view + gesture + name (P6'-P10'). Doors vary the start pose
(SP1-SP10, SP1'-SP10'). Episode ID format `S1-R1-T2-C4`.

The data is in `data/S1-R1/` (`ep_001`-`ep_130`; `test/` is not an episode), written by the recorder
of commit c3e6d3a: `episode.json` has `t_start` / `t_end` only (the instruction was typed after the
drive), so the export takes "driving starts" from the odometry. The recording order did not always
follow the protocol, so conditions, blocks and spots are not known per episode; results are broken
down by what the data itself shows instead: first-turn direction and size, object named or not
(wording), "come" or "go".

### What the data showed (2026-11-03)

- **Odometry is late.** It reaches the recorder later than the camera frames: about 2.6 s in most
  episodes (2.35-2.85 s), 0-1.5 s in a few (ep_001, 017, 018, 034, 036, 050, 125). Measured per
  episode by matching the image's sideways shift with the odometry's turn rate
  (`data_pipeline/real/sync.py`) and corrected before labelling. Checked by eye on several episodes.
- **The end of most drives is missing.** The recording stopped while the odometry was still seconds
  behind, so in most episodes the odometry ends with Roxy still moving. The pointing, the first turn and
  the first steps survive; the last steps and the stop mostly do not. Only 16 of the 118 used episodes
  have their stop.
- **Left out (12):** no odometry (ep_030), robot never moves in the odometry (ep_031), lag not
  measurable (camera gaps, ep_020, 032, 033, 121, 122), odometry gap (ep_016), driving starts before
  2 pointing frames (ep_019, 035), nothing completed before the odometry ends (ep_060, 077).
- **For the next collection:** keep recording after the second Enter until the odometry shows Roxy
  stopped (or 3 s longer). **Before testing on Roxy:** `navila_node` closes its turns on odometry; with
  seconds of lag it would overshoot, so measure the lag on the live setup first.

### Export result (`make export SESSION=S1-R1`, seed 0)

| | Episodes | With stop | Records | First-turn groups |
|---|---|---|---|---|
| train | 95 + 95 mirrored | 13 + 13 | 666 | left 37 small / 52 large, right 37 / 52, straight 12 |
| eval | 23 | 3 | 77 | left 4 / 5, right 5 / 8, straight 1 |

Train answers: turn left 36%, turn right 36%, forward 20%, stop 7.8% (stop records repeated 2x to
match RxR's stop share). Without mirroring: 333 records, right 43% / left 29%.

## Where the data lives and moves

```
laptop:       data/sessions/session_*/ep_*/   raw bags (never uploaded)
                 -> export (CPU, minutes)  -> data/navila_mix/deictic/   frames + train.json / eval.json
workstation:  NaVila/NaVILA/NaVILA-Dataset/RxR/   full RxR (annotations + 23 GB frames)
                 -> make rxr-replay  (DONE)  -> data/navila_mix/rxr_replay.json, rxr_eval.json, rxr/
both:         rsync data/navila_mix/ -> ENTC server (a few hundred MB to ~1 GB)
ENTC server:  NaVILA code (fork) + base model from Hugging Face (a8cheng/navila-llama3-8b-8f, ~16 GB)
                 -> LoRA adapter (small) -> back to the workstation for Roxy
```

## RxR replay set (done, on the workstation)

`make rxr-replay` (`data_pipeline/export/rxr_replay.py`, env `deictic_export` from
`data_pipeline/export/requirements.txt`) picked whole RxR trajectories with seed 0 and copied only
their frames from the local RxR, instead of the notebook's 24 GB download (its steps 1-3):

| File in `data/navila_mix/` | Content |
|---|---|
| `rxr_replay.json` | 100 trajectories, 6,392 records: mixed into training |
| `rxr_eval.json` | 50 other trajectories, 3,432 records: what fine-tuning forgets (base model saw them) |
| `rxr/` | 19,551 frames (558 MB), the image folder of both sets |
| `rxr_report.json` | seed, trajectory ids, counts |

Checked: no overlap, no missing frames, records identical to the source, stop share 7.8% (RxR 8.0%).
The mix step can use fewer replay records than this; it must read these files (image path
`navila_mix/rxr`) rather than pick or download again.

## Next steps

1. ~~Check the data~~ (done: pointing visible and inside the crop; `image_mode: crop` kept).
2. ~~Map episodes to protocol IDs~~ (dropped: order unreliable; random split by first-turn group).
3. ~~Export~~ (done: `make export`, lag-corrected, `path_stats.py` checks, `data/navila_mix/deictic/`).
   Train episodes also mirrored (666 records). Replay: ~2,000 RxR records per epoch, ours x2.
4. ~~Training script for the server~~ (written: `make train-setup`, `make train`, below. The data
   check and the attention swap pass on the workstation; not yet run on aberdeen).
5. **Smoke run** on aberdeen (`make train SMOKE=1`): speed, memory, loss going down.
6. **Evaluation script**: base vs fine-tuned on the 23 test episodes. First-turn direction and angle
   error, per-step action accuracy, stop accuracy; results per first-turn group, object named or not,
   "come" or "go"; the same with the pointing frames replaced (does the model use the gesture?).
7. **Full training run**, then evaluation, then the adapter on Roxy via `make infer`.

## The ENTC server (aberdeen)

2x Quadro RTX 6000 (24 GB) + 1x Quadro RTX 5000 (16 GB), 518 GB free disk, no conda, no SLURM.
The cards are Turing: no bf16 and no FlashAttention 2. NaVILA's training code assumes both
(`train.py` hard-codes `attn_implementation="flash_attention_2"`, and its patched Llama in
`transformers_replace` always uses the flash-attention class), so training there needs fp16 and
PyTorch's own attention (sdpa), set by our launcher rather than by editing the fork. The LoRA adapter
it produces is the same either way and runs with flash-attention at inference.

Internet works there (Hugging Face, PyPI, GitHub). No root, no pip, Python 3.12 only: the setup puts
Miniforge (conda) with a Python 3.10 env in user space.

### Training on aberdeen

Folder layout (`ROOT` = the folder holding the repo clone, e.g. `~/deictic`):

```
~/deictic/Diectic-VLA-FYP/                 this repo, branch data/deictic-finetune
~/deictic/Diectic-VLA-FYP/data/navila_mix/ copied from the workstation: deictic/, rxr_replay.json, rxr/
~/deictic/miniforge3/, NaVILA/, models/    made by make train-setup
~/deictic/runs/<RUN>-lora/                 adapter, checkpoints, train.log
```

1. `git clone` the repo into `~/deictic`, `git checkout data/deictic-finetune`; copy `data/navila_mix/`
   (from `make export` + `make rxr-replay`) into its `data/`.
2. `make train-setup` (or `bash training/envs/aberdeen_setup.sh`): Miniforge, env `navila-train`
   (Python 3.10, CUDA 12.1 toolkit, torch 2.3.0, NaVILA's packages at the workstation env's versions,
   NaVILA's patched transformers), the NaVILA fork at 6a8aef1, the base model `a8cheng/navila-llama3-8b-8f`.
3. `make train CHECK=1`: data and attention check, no model (~1 min).
4. `make train SMOKE=1` inside tmux: 20 steps. Gives seconds per step, GPU memory, loss going down.
5. `make train` inside tmux: full run (`RUN=`, `EPOCHS=`, `LR=`, `GPUS=` to change). Interrupted runs
   resume from the last checkpoint (every 50 steps).
6. Copy `runs/<RUN>-lora/` back for evaluation and Roxy.

`training/navila_lora.py` runs NaVILA's own trainer (`llava/train/train.py`) and only adds: the
`deictic` and `rxr_replay` datasets for `--data_mixture`, the attention swap on pre-Ampere GPUs (checked
against NaVILA's FlashAttention path: same output to fp16 precision, with padding, packed sequences and
generation), and `train_mem.py`'s patches. Settings (`training/jobs/aberdeen_lora.sh`): 4-bit NF4 base,
LoRA r16 / alpha 32 / dropout 0.05 on the language model only (vision tower and projector frozen),
lr 1e-4 cosine, 1 sample per GPU x 2 GPUs x 8 accumulation = 16 per step, 1 epoch (~210 steps).

## Open questions

- Speed and memory on the RTX 6000s (smoke run).
- Where the per-episode collection log is (episode ID, target spot, instructor position, result).
