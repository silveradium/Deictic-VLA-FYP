# Fine-tuning NaVILA on our pointing episodes

Plan and current state, so work can continue on any machine or in a new session (a new Claude session
should start by reading this file). Branch: `data/deictic-finetune`. The question: does LoRA
fine-tuning on our real Go2 episodes teach NaVILA to go where a person points, and by how much
compared with the base model?

## Status (2026-11-03)

| Step | State | Where |
|---|---|---|
| RxR replay set | done (`make rxr-replay`) | workstation |
| Export of Session 1 to NaVILA format | done (`make export SESSION=S1-R1`), checked by eye | workstation |
| Data to the laptop | done: `navila_mix.tar` (732 MB) + `SHA256SUMS` on the pendrive | pendrive |
| Training scripts | written (`make train-setup`, `make train`); data check and attention swap pass on the workstation | repo |
| Setup on aberdeen | **next** | aberdeen, via the laptop |
| Smoke run (20 steps) | to do | aberdeen |
| Full training run | to do | aberdeen |
| Evaluation script + results | to do (spec below) | aberdeen |
| Adapter on Roxy | to do (needs LoRA loading in the NaVILA server, and the odometry lag fixed) | workstation |

## Next steps

### 1. Laptop: get the code and the data

```bash
git pull                                   # branch data/deictic-finetune, after it was pushed
cd "/media/<user>/UBUNTU 24_0/deictic" && sha256sum -c SHA256SUMS
tar -xf navila_mix.tar -C <repo>/data/     # -> data/navila_mix/{deictic/,rxr/,rxr_replay.json,...}
```

The pendrive's `deictic-finetune.bundle` is an old state of the branch: use GitHub instead.

### 2. aberdeen: setup and check

Folder layout (`ROOT` = the folder holding the repo clone):

```
~/deictic/Diectic-VLA-FYP/                 this repo, branch data/deictic-finetune
~/deictic/Diectic-VLA-FYP/data/navila_mix/ from the tar
~/deictic/miniforge3/, NaVILA/, models/    made by make train-setup
~/deictic/runs/<RUN>-lora/                 adapter, checkpoints, train.log
```

```bash
mkdir -p ~/deictic && cd ~/deictic
git clone https://github.com/silveradium/Diectic-VLA-FYP.git && cd Diectic-VLA-FYP
git checkout data/deictic-finetune
# from the laptop: rsync -a <repo>/data/navila_mix/ agra@<aberdeen>:~/deictic/Diectic-VLA-FYP/data/navila_mix/
make train-setup        # once, 20-40 min, ~30 GB; safe to re-run
make train CHECK=1      # ~1 min, no model
```

Without `make`: `bash training/envs/aberdeen_setup.sh`, `CHECK=1 bash training/jobs/aberdeen_lora.sh`.
`CHECK=1` should print: 2 GPUs (compute capability 7.5), attention `sdpa`, records
`{'deictic': 666, 'rxr_replay': 2016, 'rxr trajectories': 29}`, `missing frames: 0`, and one sample
of each dataset with images `(8, 3, 384, 384)`.

### 3. aberdeen: smoke run

```bash
tmux new -s train       # training must survive the ssh session closing
make train SMOKE=1      # 20 optimizer steps, output in ~/deictic/runs/smoke-<date>-lora/
```

In a second window, `watch -n 5 nvidia-smi` for memory (expected roughly 12-16 GB per card). From the
log, note: seconds per step, the loss over the 20 steps (should go down), and any warning. An epoch is
about 210 steps (1,332 deictic + 2,016 RxR samples / 16 per step), so the full run takes about
210 x (seconds per step).

This is the first time NaVILA's training code runs on Turing cards and in this environment. Likely
problems and what to try:

| Symptom | Try |
|---|---|
| setup: `conda install ... cuda-toolkit` fails or is very slow | install only `cuda-nvcc cuda-cudart-dev` from the same channel (nvcc for deepspeed's install, libcudart for bitsandbytes) |
| `bitsandbytes` "CUDA Setup failed" / no 4-bit | `export LD_LIBRARY_PATH=$CONDA_PREFIX/lib:$LD_LIBRARY_PATH`; or `BNB_CUDA_VERSION=121` |
| "FlashAttention only supports Ampere GPUs or newer" | something other than the Llama attention calls flash: see the traceback; extend the swap in `training/navila_lora.py` |
| DDP error ("mark a variable ready only once", unused parameters) | first confirm the rest works on one GPU: `GPUS=0 GRAD_ACC=16 make train SMOKE=1`; then fix DDP (reentrant gradient checkpointing with DDP is the usual cause) |
| loss `nan` (fp16 overflow) | `LR=5e-5`; check which module overflows |
| out of memory | shouldn't happen at 1 sample per GPU; report the numbers |

### 4. aberdeen: full run

```bash
make train              # -> ~/deictic/runs/deictic-s1r1-lora/ (RUN=, EPOCHS=, LR=, GPUS= to change)
```

Checkpoints every 50 steps; starting it again with the same `RUN` resumes. A folder that holds a
finished run makes NaVILA skip training: use a new `RUN` name for a new run. The adapter
(`adapter_model.*`, `adapter_config.json`, `non_lora_trainables.bin`, `config.json`) is small; copy the
run folder back through the laptop.

### 5. Evaluation script (to write: `training/eval_navila.py`, run on aberdeen)

Base model vs fine-tuned adapter, same inputs, greedy decoding.

- **Loading.** Base: `quick_infer.load_model(MODEL_DIR, "fp16")` (`NaVILA/scripts/inference/quick_infer.py`;
  fp16 8B fits a 24 GB card). Adapter: `llava.model.builder.load_pretrained_model(run_dir, name,
  model_base=MODEL_DIR)` (the path must contain `lora`; NaVILA's LoRA loading is unexercised, so check
  that the adapter weights really load: compare outputs with and without it). On Turing call
  `navila_lora.use_sdpa_if_needed("auto")` before loading; the swap also handles generation (tested).
- **Inputs.** Each record of `deictic/eval.json` (77 records, 23 episodes): frames from
  `deictic/frames/`, sampled to 8 with `llava.mm_utils.vlnce_frame_sampling` as in training; prompt =
  `quick_infer.predict`'s (identical to the training prompt in `LazyVLNCEDataset`). Parse with
  `navila_go2.common.mid_level.parse_navila_output`.
- **Metrics.** Headline: the first decision of each episode (record `-0`): turn direction correct
  (left / right / forward) and angle error. Then per record: action type accuracy, exact answer, angle
  error for turns, distance error for forward, stop recall (only 3 eval episodes have a stop: report,
  don't over-read). Break down by the episode fields in `deictic/export_report.json` (`per episode`):
  `turn group`, `named`, `verb`.
- **Gesture ablation.** The same with the pointing frames (the first `pointing frames` of each history)
  taken out. Open decision: drop them or replace them with copies of the frame at go. The frame at go
  can still show the arm, so look at a few first.
- **Forgetting.** A few hundred records of `rxr_eval.json` (whole trajectories, frames in `rxr/`; the
  base model trained on these): action type accuracy and exact answer, base vs fine-tuned.
- **Output.** JSON with every prediction + a markdown table, in `runs/<RUN>-lora/eval/`.

### 6. The adapter on Roxy

- The NaVILA server (`navila_go2/server/navila_server.py`, via `quick_infer.load_model`) has no
  `model_base` argument, so it can't load a LoRA adapter yet. Either add one, or merge the adapter
  into the base weights once (`merge_and_unload`) and serve the merged model.
- **Before moving Roxy:** `navila_node` closes its turns on odometry, and the odometry arrived ~2.6 s
  late in Session 1 (below). Measure the lag on the live setup first, or turns will overshoot.

### Later

- Recorder: keep recording after the second Enter until the odometry shows Roxy stopped (or 3 s
  longer), so drives keep their endings and stops; log `t_go`. Keep a collection log (episode -> target,
  condition) so results can be broken down by T0-T6 and blocks.
- Instruction typos: `ep_012` "CoGo there and stop" (eval), `ep_065` "\Go there and stop" (train). Fix
  in `episode.json`, re-export, re-tar (raw data and the export env are on the workstation).
- More sessions / rooms / people; the ambiguous-target episodes; DeePoint augmentation.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Labels | NaVILA's exact training wording, made from odometry at export time: `The next action is turn left 45 degree.`, `... move forward 75 cm.`, `I think I should stop because I have finished the instruction.` | 130 episodes is too few to also teach a new output format; keeps the base model comparable |
| Model input | Same rules as inference (`roxy.yaml` navila_node: `image_mode: crop`, `gesture_frames: 4`, `gesture_window_s: 3.0`). Pointing frames go first in the history; NaVILA's frame sampling always keeps frame 0 | What the model is trained on must match what Roxy gives it |
| Test split | Random 20% of episodes (seed 0), drawn within each first-turn group (left / right x under / over 45 degrees, straight) | Recording order did not always follow the protocol, so spots can't be recovered; the groups keep left/right and small/large turns balanced in the test set |
| Missing endings | Keep only actions the odometry saw completed; no stop record when the stop was not seen | The odometry lag cut off the end of most drives (below); invented endings would be worse than fewer labels |
| Mirroring | Every train episode is added a second time mirrored: frames flipped left-right, turn left / right swapped (`--no-flip` to leave out). Eval episodes are never mirrored | A mirrored episode is a valid one (person points the other way, robot turns the other way; frames are cropped about the heading). Doubles the train set and balances left / right, but adds no new rooms, people or targets |
| Replay | RxR only: per epoch our 666 train records x2 plus ~2,000 RxR records (29 whole trajectories of `rxr_replay.json`), so ours are ~40% | Against forgetting general navigation, without repeating our small set 10x+; R2R would be a 20 GB download |
| Training | LoRA r16 / alpha 32 / dropout 0.05 on the language model only, 4-bit NF4 base, vision tower and projector frozen; lr 1e-4 cosine, warmup 3%; 16 samples per step; 1 epoch | Small dataset; fits a 24 GB card; a first run to compare against the base model |
| Hardware | aberdeen (ENTC): 2x Quadro RTX 6000 24 GB, one process per GPU (`torchrun`), fp16 + PyTorch attention (Turing) | The workstation is needed for other work; only the laptop reaches aberdeen |

## The dataset (Session 1)

One room (HQ living room), one person, 130 episodes, fixed wording ("go there", "go to that chair").
Protocol: T0 empty floor (10 episodes, P1-P10). T1 dustbin, T2 chair, T3 shoes, T4 fan, T5 brooms with
distractors, T6 doors (20 each). Blocks of 5: A in view + gesture, B in view + gesture + name (spots
P1'-P5'), C out of view + gesture, D out of view + gesture + name (P6'-P10'). Doors vary the start pose
(SP1-SP10, SP1'-SP10').

Raw episodes: `data/S1-R1/` on the workstation (`ep_001`-`ep_130`, 1.3 GB; `test/` is not an episode),
written by the recorder of commit c3e6d3a: `episode.json` has `t_start` / `t_end` only (the instruction
was typed after the drive), so the export takes "driving starts" from the odometry. The recording order
did not always follow the protocol, so conditions, blocks and spots are not known per episode; results
are broken down by what the data itself shows: first-turn direction and size, object named or not,
"come" or "go".

### What the data showed

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

### Export result (`make export SESSION=S1-R1`, seed 0)

| | Episodes | With stop | Records | First-turn groups |
|---|---|---|---|---|
| train | 95 + 95 mirrored | 13 + 13 | 666 | left 37 small / 52 large, right 37 / 52, straight 12 |
| eval | 23 | 3 | 77 | left 4 / 5, right 5 / 8, straight 1 |

Train answers: turn left 36%, turn right 36%, forward 20%, stop 7.8% (stop records repeated 2x to
match RxR's stop share). Without mirroring: 333 records, right 43% / left 29%. Per-episode details
(split, turn group, lag, labels, problems): `data/navila_mix/deictic/export_report.json`.

## Where the data lives and moves

```
workstation:  data/S1-R1/                         raw episodes (bags + episode.json), 1.3 GB
                 -> make export      -> data/navila_mix/deictic/   frames, train.json, eval.json, report
              NaVila/NaVILA/NaVILA-Dataset/RxR/   full RxR (annotations + 23 GB frames)
                 -> make rxr-replay  -> data/navila_mix/rxr_replay.json, rxr_eval.json, rxr/
              data/navila_mix/ -> pendrive navila_mix.tar (732 MB, SHA256SUMS)
laptop:       the only machine that reaches aberdeen: tar -> data/navila_mix/ -> rsync to aberdeen
aberdeen:     NaVILA fork + base model from Hugging Face (make train-setup) -> runs/<RUN>-lora/
                 -> adapter (small) back through the laptop to the workstation for Roxy
```

Both export targets run on the host in the conda env `deictic_export`
(`data_pipeline/export/requirements.txt`; see the Makefile). Re-exporting needs the workstation.

### RxR replay set

`make rxr-replay` (`data_pipeline/export/rxr_replay.py`) picked whole RxR trajectories with seed 0 and
copied only their frames from the local RxR, instead of the notebook's 24 GB download:

| File in `data/navila_mix/` | Content |
|---|---|
| `rxr_replay.json` | 100 trajectories, 6,392 records; training uses ~2,000 of them (whole trajectories, seed 0) |
| `rxr_eval.json` | 50 other trajectories, 3,432 records: what fine-tuning forgets (the base model saw them) |
| `rxr/` | 19,551 frames (558 MB), the image folder of both sets |
| `rxr_report.json` | seed, trajectory ids, counts |

Checked: no overlap, no missing frames, records identical to the source, stop share 7.8% (RxR 8.0%).

## Training code

- `training/envs/aberdeen_setup.sh` (`make train-setup`): Miniforge in user space (aberdeen has no
  root, pip, conda or Docker, and Python 3.12 only), env `navila-train`: Python 3.10, CUDA 12.1 toolkit,
  torch 2.3.0, NaVILA's packages at the versions of the workstation's working env
  (`docker/navila/constraints.txt`), NaVILA's patched transformers / deepspeed files, the flash-attn
  wheel (imported by NaVILA's code, never run on Turing); the fork `silveradium/NaVILA` at 6a8aef1 (its
  `llava/` = upstream 76b98f2); the base model `a8cheng/navila-llama3-8b-8f`. Not yet run anywhere:
  running the Miniforge installer on the workstation was blocked.
- `training/jobs/aberdeen_lora.sh` (`make train`): the torchrun command and all settings, overridable
  from the environment; `CHECK=1` and `SMOKE=1` modes.
- `training/navila_lora.py`: runs NaVILA's own trainer (`llava/train/train.py`, as `train_mem.py` does)
  and adds three things without editing the fork:
  - It registers the `deictic` and `rxr_replay` datasets for `--data_mixture`, and writes the RxR subset
    to the run folder.
  - It swaps the attention kernel on GPUs older than Ampere. NaVILA's patched Llama always calls
    FlashAttention 2, so only that call is replaced by PyTorch's `scaled_dot_product_attention`, with the
    same masking. It was checked against NaVILA's real flash path on the workstation: same output to
    fp16 precision, with padding, packed sequences and generation. The adapter is the same either way.
  - It applies `train_mem.py`'s patches.

  Checked on the workstation: data check passes, all job arguments parse with NaVILA's parser. The
  workstation's `navila` conda env is incomplete (no PIL, pandas, sympy), so local tests there need
  those packages from a separate `--target` folder.

## Open questions

- Speed and memory on the RTX 6000s (smoke run); then the full run's length (epochs).
- Gesture ablation: drop the pointing frames or replace them (step 5).
- Serving the adapter: `model_base` in the NaVILA server or a merged model (step 6).
- Where the per-episode collection log is (episode ID, target spot, instructor position, result).
