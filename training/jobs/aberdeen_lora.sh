#!/usr/bin/env bash
# LoRA fine-tuning of NaVILA on our pointing episodes + RxR replay, on aberdeen (ENTC): the two
# Quadro RTX 6000 (24 GB) cards, one process each (torchrun), base model in 4 bits, fp16 (Turing: no
# bf16, no FlashAttention; training/navila_lora.py handles that). Setup first:
# training/envs/aberdeen_setup.sh. Run inside tmux, the full run takes hours:
#
#   CHECK=1 training/jobs/aberdeen_lora.sh   data + attention check, no model (~1 min)
#   SMOKE=1 training/jobs/aberdeen_lora.sh   20 steps: speed, GPU memory, loss going down
#           training/jobs/aberdeen_lora.sh   full run -> $ROOT/runs/$RUN-lora (adapter + train.log)
#
# Any setting below can be changed from the environment, e.g. EPOCHS=2 LR=2e-4 training/jobs/...
# A full run's output folder must not hold a finished run (NaVILA then skips training); an
# interrupted run resumes from its last checkpoint when started again with the same RUN.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ROOT=${ROOT:-$(dirname "$REPO")}
NAVILA_DIR=${NAVILA_DIR:-$ROOT/NaVILA}
MODEL_DIR=${MODEL_DIR:-$ROOT/models/navila-llama3-8b-8f}
MIX=${MIX:-$REPO/data/navila_mix}  # from the workstation: deictic/, rxr_replay.json, rxr/
GPUS=${GPUS:-0,1}                  # nvidia-smi indices: 0, 1 = RTX 6000 (2 = the 16 GB RTX 5000)
RUN=${RUN:-deictic-s1r1}

DEICTIC_REPEAT=${DEICTIC_REPEAT:-2}  # our train records (666 with mirrored) seen this often per epoch
RXR_RECORDS=${RXR_RECORDS:-2000}     # RxR replay records per epoch, whole trajectories (forgetting)
EPOCHS=${EPOCHS:-1}
LR=${LR:-1e-4}
LORA_R=${LORA_R:-16}
LORA_ALPHA=${LORA_ALPHA:-32}
GRAD_ACC=${GRAD_ACC:-8}              # 1 sample per GPU x 2 GPUs x 8 = 16 samples per optimizer step
PORT=${PORT:-29511}

# shellcheck disable=SC1091
source "$ROOT/miniforge3/etc/profile.d/conda.sh"
conda activate "${ENV_NAME:-navila-train}"
export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=$GPUS
export TOKENIZERS_PARALLELISM=false WANDB_DISABLED=true
cd "$NAVILA_DIR"

if [ -n "${CHECK:-}" ]; then
    exec python "$REPO/training/navila_lora.py" --check --mix "$MIX" --rxr-records "$RXR_RECORDS" \
        --model_name_or_path "$MODEL_DIR" --output_dir "$ROOT/runs/check-lora"
fi

if [ -n "${SMOKE:-}" ]; then
    OUT=${OUT:-$ROOT/runs/smoke-$(date +%m%d-%H%M)-lora}
    length=(--max_steps 20 --save_strategy no)
else
    OUT=${OUT:-$ROOT/runs/$RUN-lora}
    length=(--num_train_epochs "$EPOCHS" --save_strategy steps --save_steps 50 --save_total_limit 2)
fi
mkdir -p "$OUT"
NPROC=$(tr ',' '\n' <<<"$GPUS" | grep -c .)
echo "run $OUT on GPUs $GPUS: deictic x$DEICTIC_REPEAT + $RXR_RECORDS RxR, lr $LR, LoRA r$LORA_R" | tee -a "$OUT/train.log"

torchrun --nproc_per_node="$NPROC" --master_port="$PORT" "$REPO/training/navila_lora.py" \
    --mix "$MIX" --rxr-records "$RXR_RECORDS" \
    --model_name_or_path "$MODEL_DIR" --version llama_3 --seed 0 \
    --data_mixture "deictic*$DEICTIC_REPEAT+rxr_replay" \
    --vision_tower "$MODEL_DIR/vision_tower" --mm_vision_select_feature cls_patch \
    --mm_projector mlp_downsample --mm_vision_select_layer -2 \
    --mm_use_im_start_end False --mm_use_im_patch_token False \
    --num_video_frames 8 --image_aspect_ratio resize --fps 0.0 \
    --tune_vision_tower False --tune_mm_projector False --tune_language_model False \
    --lora_enable True --lora_llm True --lora_r "$LORA_R" --lora_alpha "$LORA_ALPHA" --lora_dropout 0.05 \
    --bits 4 --quant_type nf4 --double_quant True \
    --fp16 True --bf16 False --tf32 False \
    --output_dir "$OUT" "${length[@]}" \
    --per_device_train_batch_size 1 --gradient_accumulation_steps "$GRAD_ACC" \
    --learning_rate "$LR" --weight_decay 0. --warmup_ratio 0.03 --lr_scheduler_type cosine \
    --model_max_length 4096 --gradient_checkpointing True --ddp_find_unused_parameters False \
    --dataloader_num_workers 4 --lazy_preprocess True \
    --do_eval False --logging_steps 1 --report_to none \
    2>&1 | tee -a "$OUT/train.log"
