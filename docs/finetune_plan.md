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
| Replay | RxR only, about 50-100 trajectories, roughly 1 replay record per deictic record | Avoid forgetting general navigation; R2R would be a 20 GB download |
| Test split | Held-out target spots, never trained on (below), not a random split | One room, one person: a random split lets the model memorise spot -> path |

## The dataset (Session 1)

One room (HQ living room), one person, 130 episodes, fixed wording ("go there", "go to the bin").
T0 empty floor (10 episodes, P1-P10). T1 dustbin, T2 chair, T3 shoes, T4 fan, T5 brooms with
distractors, T6 doors (20 each). Blocks of 5: A in view + gesture, B in view + gesture + name (spots
P1'-P5'), C out of view + gesture, D out of view + gesture + name (P6'-P10'). Doors vary the start pose
(SP1-SP10, SP1'-SP10'). Episode ID format `S1-R1-T2-C4`.

**Test episodes (26):** T0 at P5 and P10; T1-T5 at P5 (A), P5' (B), P10 (C), P10' (D); T6 at SP5,
SP5', SP10, SP10'. Everything else (104) is training.

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

1. **Check the data.** Sessions into `data/sessions/`, `make preview SESSION=...` per session. In the
   "first decision sees" panel the person and the pointing arm must be inside the dashed crop box;
   if often not, switch `image_mode` to `squash` in `roxy.yaml` before exporting.
2. **Map episodes to protocol IDs.** Each `ep_NNN` needs its `S1-R1-Tx-yN` ID (from the collection log
   or the folder names) for the test split and per-condition results.
3. **Export changes** (`data_pipeline/export/`):
   - test split from the held-out spots instead of `EVAL_FRACTION`;
   - strafing / reversing checks from `path_stats.py` instead of `odom_actions.py`'s simpler lateral
     check, which flags curved walking and would drop good episodes under `STRICT`;
   - mix step: read the RxR replay set above, skip the notebook's download steps. It needs the replay
     record count (from `rxr_report.json`) for the repeat factor; the frames only have to be on the
     training machine.
4. **Training script for the server**: `--bits 4`, no DeepSpeed ZeRO-3, `torchrun --nproc_per_node=2`,
   per-GPU batch 1-2 with gradient accumulation, `output_dir` containing `lora` (NaVILA's loader
   only loads LoRA weights from such paths). bf16 and flash-attention need an Ampere or newer GPU.
5. **Smoke run** of about 20 steps on the server: speed, memory, loss going down.
6. **Evaluation script**: base vs fine-tuned on the 26 test episodes. First-turn direction and angle
   error, per-step action accuracy, stop accuracy; results per condition (T0-T6) x block (A-D), near
   and far separately; the same with the pointing frames replaced (does the model use the gesture?).
7. **Full training run**, then evaluation, then the adapter on Roxy via `make infer`.

## Open questions

- ENTC server: GPU model, SLURM or not, conda or containers, internet access, free disk (~30 GB).
- Where the per-episode collection log is (episode ID, target spot, instructor position, result).
