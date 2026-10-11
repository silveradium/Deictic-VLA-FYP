# syntax=docker/dockerfile:1
# NaVILA high-level policy server (CUDA, x86). Inference only: training runs on the
# servers in a conda/uv env (training/envs).
#
# Only dependencies are baked in. At runtime the NaVILA fork is mounted read-only at
# /workspace/NaVILA and the weights at /checkpoints, so code edits and new checkpoints
# (e.g. LoRA adapters) need no rebuild. Rebuild only when the fork's pyproject.toml or
# llava/train/{transformers,deepspeed}_replace change.
#
# Build contexts (docker/compose/navila.yml):
#   default  docker/navila   (constraints.txt)
#   navila   $NAVILA_DIR     (only pyproject.toml and the *_replace patches are read)
FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# git: s2wrapper installs from GitHub. libgl1/libglib2.0-0: opencv-python.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3-pip python-is-python3 git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install torch==2.3.0 torchvision==0.18.0 --index-url https://download.pytorch.org/whl/cu121

# VILA's dependency list, taken from the fork's pyproject (the package itself is the mount)
COPY constraints.txt /tmp/constraints.txt
COPY --from=navila pyproject.toml /tmp/vila/pyproject.toml
RUN pip install tomli \
    && python -c "import tomli; print('\n'.join(tomli.load(open('/tmp/vila/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/vila/requirements.txt \
    && pip install -r /tmp/vila/requirements.txt -c /tmp/constraints.txt

# llava imports deepspeed.comm at module load (llava/train/sequence_parallel), even for
# inference. Its setup.py runs `nvcc -V`, so add the compiler (the runtime base has none).
# Ops are JIT-compiled on demand, and inference never needs them.
RUN apt-get update && apt-get install -y --no-install-recommends cuda-nvcc-12-1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install deepspeed==0.9.5 -c /tmp/constraints.txt

# flash-attn is imported at module load too (InternViT encoder). Same prebuilt wheel as
# environment_setup.sh, matching torch 2.3 / cp310.
RUN pip install https://github.com/Dao-AILab/flash-attention/releases/download/v2.5.8/flash_attn-2.5.8+cu122torch2.3cxx11abiFALSE-cp310-cp310-linux_x86_64.whl

# NaVILA's patched transformers 4.37.2 and deepspeed files (same step as environment_setup.sh)
COPY --from=navila llava/train/transformers_replace/ /tmp/transformers_replace/
COPY --from=navila llava/train/deepspeed_replace/ /tmp/deepspeed_replace/
# (find_spec, not import: importing deepspeed prints to stdout)
RUN pkgdir() { python -c "import importlib.util as u, os; print(os.path.dirname(u.find_spec('$1').origin))"; } \
    && cp -r /tmp/transformers_replace/* "$(pkgdir transformers)/" \
    && cp -r /tmp/deepspeed_replace/* "$(pkgdir deepspeed)/" \
    && rm -rf /tmp/transformers_replace /tmp/deepspeed_replace

WORKDIR /workspace/NaVILA
EXPOSE 8000
CMD ["python", "navila_go2/server/navila_server.py", "--model-path", "/checkpoints/navila-llama3-8b-8f", "--host", "0.0.0.0"]
