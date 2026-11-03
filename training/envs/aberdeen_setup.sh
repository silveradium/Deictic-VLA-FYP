#!/usr/bin/env bash
# One-time setup for LoRA training on aberdeen (ENTC). That machine has no root access, no conda and
# no Docker, so everything goes in user space under $ROOT (default: the folder holding this repo):
#
#   $ROOT/miniforge3/                      conda (conda-forge); env navila-train: Python 3.10, CUDA 12.1
#                                          toolkit, NaVILA's training packages at the versions of the
#                                          workstation's working env (docker/navila/constraints.txt)
#   $ROOT/NaVILA/                          the NaVILA fork (training reads its llava/ code)
#   $ROOT/models/navila-llama3-8b-8f/      the base model from Hugging Face (~16 GB)
#
# Same steps as the fork's environment_setup.sh, plus the base model. Safe to re-run: finished steps
# are skipped, an interrupted model download resumes. About 30 GB and 20-40 min. Then:
#   CHECK=1 training/jobs/aberdeen_lora.sh     (once the data is in data/navila_mix)
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ROOT=${ROOT:-$(dirname "$REPO")}
ENV_NAME=${ENV_NAME:-navila-train}
NAVILA_DIR=${NAVILA_DIR:-$ROOT/NaVILA}
NAVILA_URL=${NAVILA_URL:-https://github.com/silveradium/NaVILA.git}
NAVILA_REF=${NAVILA_REF:-6a8aef1}  # fork main (2026-10); its llava/ is upstream 76b98f2 unchanged
MODEL_DIR=${MODEL_DIR:-$ROOT/models/navila-llama3-8b-8f}
HF_MODEL=a8cheng/navila-llama3-8b-8f
CONSTRAINTS=$REPO/docker/navila/constraints.txt
FLASH_WHEEL=https://github.com/Dao-AILab/flash-attention/releases/download/v2.5.8/flash_attn-2.5.8+cu122torch2.3cxx11abiFALSE-cp310-cp310-linux_x86_64.whl

step() { printf '\n=== %s\n' "$*"; }

step "1/6 conda (Miniforge) in $ROOT/miniforge3"
if [ ! -x "$ROOT/miniforge3/bin/conda" ]; then
    installer=$ROOT/Miniforge3-Linux-x86_64.sh
    curl -fL -o "$installer" https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
    bash "$installer" -b -p "$ROOT/miniforge3"
fi
# shellcheck disable=SC1091
source "$ROOT/miniforge3/etc/profile.d/conda.sh"

step "2/6 env $ENV_NAME: Python 3.10 + CUDA 12.1 toolkit (nvcc for deepspeed's install, libcudart for bitsandbytes)"
conda env list | awk '{print $1}' | grep -qx "$ENV_NAME" || conda create -y -n "$ENV_NAME" python=3.10
conda activate "$ENV_NAME"
[ -x "$CONDA_PREFIX/bin/nvcc" ] || conda install -y -c nvidia/label/cuda-12.1.1 cuda-toolkit
export CUDA_HOME=$CONDA_PREFIX

step "3/6 PyTorch 2.3.0 for CUDA 12.1"
python -c "import torch" 2>/dev/null \
    || pip install torch==2.3.0 torchvision==0.18.0 --index-url https://download.pytorch.org/whl/cu121

step "4/6 NaVILA fork ($NAVILA_REF) and its training packages"
[ -d "$NAVILA_DIR/.git" ] || git clone "$NAVILA_URL" "$NAVILA_DIR"
git -C "$NAVILA_DIR" checkout -q "$NAVILA_REF"
if ! python -c "import llava, deepspeed, flash_attn" 2>/dev/null; then
    pip install -e "$NAVILA_DIR" -c "$CONSTRAINTS"
    pip install deepspeed==0.9.5 ninja -c "$CONSTRAINTS"
    # Imported by NaVILA's code. Its kernels need an Ampere GPU and are never called on aberdeen's Turing
    # cards: training/navila_lora.py swaps them for PyTorch attention.
    pip install "$FLASH_WHEEL"
    # NaVILA's patched transformers 4.37.2 / deepspeed files (environment_setup.sh does the same)
    site=$(python -c 'import site; print(site.getsitepackages()[0])')
    cp -r "$NAVILA_DIR"/llava/train/transformers_replace/* "$site/transformers/"
    cp -r "$NAVILA_DIR"/llava/train/deepspeed_replace/* "$site/deepspeed/"
fi

step "5/6 base model $HF_MODEL -> $MODEL_DIR"
if [ -n "${SKIP_MODEL:-}" ]; then
    echo "skipped (SKIP_MODEL set)"
else
    python -c 'import sys; from huggingface_hub import snapshot_download as d; print(d(sys.argv[1], local_dir=sys.argv[2]))' \
        "$HF_MODEL" "$MODEL_DIR"
fi

step "6/6 GPUs and 4-bit support"
python - <<'EOF'
import torch
import bitsandbytes.nn as bnb
print("torch", torch.__version__, "CUDA", torch.version.cuda, "| bitsandbytes 4-bit:", hasattr(bnb, "Linear4bit"))
for i in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(i)
    print(f"  GPU {i}: {p.name}, {p.total_memory / 2**30:.0f} GB, compute capability {p.major}.{p.minor}")
x = torch.randn(64, 64, device="cuda", dtype=torch.float16)
print("fp16 matmul on GPU:", tuple((x @ x).shape))
EOF

echo
echo "Setup done. Next: copy data/navila_mix into $REPO/data/, then"
echo "  CHECK=1 training/jobs/aberdeen_lora.sh    data + attention check (~1 min)"
echo "  SMOKE=1 training/jobs/aberdeen_lora.sh    20 training steps"
