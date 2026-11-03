"""LoRA fine-tuning of NaVILA on our pointing episodes plus RxR replay.

Runs NaVILA's own trainer (llava/train/train.py, as llava/train/train_mem.py does) and sets three
things from outside, so the NaVILA fork stays unedited:

- Datasets. `deictic` (navila_mix/deictic/train.json, from `make export`) and `rxr_replay` (a subset of
  navila_mix/rxr_replay.json, whole trajectories, written to the output dir) are registered by name
  for --data_mixture, e.g. `deictic*2+rxr_replay`.
- Attention on GPUs older than Ampere (aberdeen's Quadro RTX 6000s are Turing). NaVILA's patched Llama
  (llava/train/transformers_replace) always calls FlashAttention 2, which needs compute capability 8.0.
  There, only the kernel call is swapped for PyTorch's scaled_dot_product_attention with the same
  masking (padding, and NaVILA's packing of several samples into one row): see sdpa_attention.
  The result is the same, so the adapter runs with FlashAttention at inference as usual.
- train_mem.py's mock patches.

Run under torchrun by training/jobs/aberdeen_lora.sh. Arguments this script doesn't define go to
NaVILA (HfArgumentParser: --model_name_or_path, --lora_enable, ...). `--check` builds both datasets,
reads samples and tests the attention swap on the GPU, without loading the model.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

import torch
import torch.nn.functional as F

DEICTIC, RXR = "deictic", "rxr_replay"


# --- attention ---------------------------------------------------------------------------------------

def segment_ids(attention_mask: torch.Tensor, seqlens_in_batch) -> torch.Tensor:
    """(batch, kv_len): which sequence each token belongs to, -1 for padding.

    Same reading as flash's unpadding (_get_unpad_data): tokens in row-major order, cut into
    sequences of seqlens_in_batch tokens (several per row when NaVILA packed them), or one per row."""
    mask = attention_mask.bool()
    lengths = mask.sum(-1) if seqlens_in_batch is None else torch.as_tensor(seqlens_in_batch, device=mask.device)
    ids = torch.repeat_interleave(torch.arange(len(lengths), device=mask.device), lengths.long())
    if ids.numel() != int(mask.sum()):
        raise ValueError(f"seqlens_in_batch {lengths.tolist()} doesn't match {int(mask.sum())} tokens")
    seg = torch.full(mask.shape, -1, dtype=torch.long, device=mask.device)
    seg[mask] = ids
    return seg


def sdpa_attention(self, query_states, key_states, value_states, attention_mask, query_length,
                   dropout=0.0, softmax_scale=None, seqlens_in_batch=None):
    """Drop-in for LlamaFlashAttention2._flash_attention_forward, with torch's SDPA.

    In: q (batch, q_len, heads, dim), k / v (batch, kv_len, kv_heads, dim), attention_mask
    (batch, kv_len) 1 = token / 0 = padding or None. Out: (batch, q_len, heads, dim), zeros at padding."""
    q, k, v = (t.transpose(1, 2) for t in (query_states, key_states, value_states))
    batch, _, q_len, _ = q.shape
    kv_len = k.shape[2]
    if k.shape[1] != q.shape[1]:  # grouped-query attention: flash maps query head i to kv head i // groups
        groups = q.shape[1] // k.shape[1]
        k, v = k.repeat_interleave(groups, dim=1), v.repeat_interleave(groups, dim=1)
    if attention_mask is None:
        attention_mask = torch.ones(batch, kv_len, dtype=torch.bool, device=q.device)
    seg_k = segment_ids(attention_mask, seqlens_in_batch)
    seg_q = seg_k[:, kv_len - q_len:]
    pos_k = torch.arange(kv_len, device=q.device)
    pos_q = pos_k[kv_len - q_len:]
    allowed = (seg_q[:, :, None] == seg_k[:, None, :]) & (seg_k[:, None, :] >= 0)
    if self.is_causal:  # flash aligns the causal mask bottom-right (q_len < kv_len while generating)
        allowed &= pos_k[None, None, :] <= pos_q[None, :, None]
    allowed |= (seg_q[:, :, None] < 0) & (pos_k[None, None, :] == pos_q[None, :, None])  # padding: no NaN rows
    out = F.scaled_dot_product_attention(q, k, v, attn_mask=allowed[:, None], dropout_p=dropout,
                                         scale=softmax_scale)
    return out.transpose(1, 2) * (seg_q >= 0)[:, :, None, None].to(out.dtype)


def use_sdpa_if_needed(mode: str) -> str:
    """Swap the flash kernel call for SDPA when mode is sdpa, or auto on a GPU older than Ampere."""
    if mode == "auto":
        mode = "flash" if torch.cuda.is_available() and torch.cuda.get_device_capability()[0] >= 8 else "sdpa"
    if mode == "sdpa":
        from transformers.models.llama import modeling_llama

        modeling_llama.LlamaFlashAttention2._flash_attention_forward = sdpa_attention
    return mode


# --- data ----------------------------------------------------------------------------------------------

def trajectory(video_id: str) -> str:
    return video_id.rsplit("-", 1)[0]


def rxr_subset(src: Path, n_records: int, seed: int) -> list[dict]:
    """Whole trajectories of src, in a seeded random order, until about n_records records."""
    by_traj = defaultdict(list)
    for r in json.loads(src.read_text()):
        by_traj[trajectory(r["video_id"])].append(r)
    order = sorted(by_traj)
    random.Random(seed).shuffle(order)
    out = []
    for t in order:
        if out and len(out) + len(by_traj[t]) / 2 > n_records:
            break
        out += by_traj[t]
    return out


def register(mix: Path, out_dir: Path, rxr_records: int, seed: int) -> dict:
    """Write the RxR subset next to the run and register both datasets with NaVILA."""
    from llava.data.datasets_mixture import Dataset, add_dataset

    deictic_json = mix / "deictic" / "train.json"
    rxr_json = out_dir / "rxr_replay_subset.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    recs = rxr_subset(mix / "rxr_replay.json", rxr_records, seed)
    tmp = rxr_json.with_suffix(f".{os.getpid()}.tmp")  # every rank writes the same file: replace atomically
    tmp.write_text(json.dumps(recs))
    os.replace(tmp, rxr_json)
    add_dataset(Dataset(dataset_name=DEICTIC, dataset_type="vlnce", data_path=str(deictic_json),
                        image_path=str(mix / "deictic" / "frames"), description="Our Go2 pointing episodes"))
    add_dataset(Dataset(dataset_name=RXR, dataset_type="vlnce", data_path=str(rxr_json),
                        image_path=str(mix / "rxr"), description="RxR replay against forgetting"))
    n_deictic = len(json.loads(deictic_json.read_text()))
    return {DEICTIC: n_deictic, RXR: len(recs), "rxr trajectories": len({trajectory(r["video_id"]) for r in recs})}


def missing_frames(mix: Path) -> list[str]:
    missing = []
    for path, folder in ((mix / "deictic" / "train.json", mix / "deictic" / "frames"),
                         (mix / "rxr_replay.json", mix / "rxr")):
        for r in json.loads(path.read_text()):
            missing += [f for f in r["frames"] if not (folder / f).exists()]
    return missing


# --- check -----------------------------------------------------------------------------------------------

def check_attention() -> str:
    """SDPA swap against plain causal attention on small random inputs (padding and packing)."""
    if not torch.cuda.is_available():
        return "skipped (no GPU)"
    dev, dt = "cuda", torch.float16
    gen = torch.Generator(device=dev).manual_seed(0)
    q = torch.randn(2, 12, 8, 16, device=dev, dtype=dt, generator=gen)
    k = torch.randn(2, 12, 2, 16, device=dev, dtype=dt, generator=gen)
    v = torch.randn(2, 12, 2, 16, device=dev, dtype=dt, generator=gen)
    mask = torch.ones(2, 12, dtype=torch.bool, device=dev)
    mask[1, 9:] = False  # row 1: 9 tokens + 3 padding
    stub = type("Attn", (), {"is_causal": True})()
    got = sdpa_attention(stub, q, k, v, mask, 12, seqlens_in_batch=[5, 7, 9])  # row 0 holds two sequences
    worst = 0.0
    for row, start, n in ((0, 0, 5), (0, 5, 7), (1, 0, 9)):
        qs, ks, vs = (t[row:row + 1, start:start + n].transpose(1, 2) for t in (q, k, v))
        ref = F.scaled_dot_product_attention(qs, ks.repeat_interleave(4, 1), vs.repeat_interleave(4, 1),
                                             is_causal=True).transpose(1, 2)
        worst = max(worst, (got[row:row + 1, start:start + n] - ref).abs().max().item())
    pad = got[1, 9:].abs().max().item()
    if worst > 1e-2 or pad != 0:
        raise SystemExit(f"attention swap check failed: max error {worst:.4f}, padding output {pad}")
    return f"ok (max error {worst:.1e})"


def check(mix: Path, model: str, out_dir: Path, rxr_records: int, seed: int):
    print("GPUs:", [f"{i}: {torch.cuda.get_device_name(i)} cc{torch.cuda.get_device_capability(i)}"
                    for i in range(torch.cuda.device_count())] or "none")
    print("attention:", use_sdpa_if_needed("auto"), "| swap check:", check_attention())
    counts = register(mix, out_dir, rxr_records, seed)
    print("records:", counts)
    missing = missing_frames(mix)
    print(f"missing frames: {len(missing)}", missing[:5])

    import transformers

    import llava.conversation as conv
    from llava.data.builder import build_dataset
    from llava.train.args import DataArguments, TrainingArguments

    # as llava/model/language_model/builder.py and train.py set them up for version llama_3
    llm = os.path.join(model, "llm")
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        llm, model_max_length=transformers.AutoConfig.from_pretrained(llm).model_max_length,
        padding_side="right", use_fast=False, legacy=False)
    tokenizer.pad_token = tokenizer.unk_token
    conv.default_conversation = conv.conv_templates["llama_3"]
    data_args = DataArguments(data_mixture=f"{DEICTIC}+{RXR}", num_video_frames=8, image_aspect_ratio="resize",
                              is_multimodal=True, lazy_preprocess=True)
    data_args.image_processor = transformers.SiglipImageProcessor.from_pretrained(os.path.join(model, "vision_tower"))
    data_args.mm_use_im_start_end = False
    train_args = TrainingArguments(output_dir=str(out_dir), model_max_length=4096)
    ds = build_dataset(data_args.data_mixture, data_args, train_args, tokenizer)
    for name, d in zip((DEICTIC, RXR), ds.datasets):
        item = d[0]
        answer = tokenizer.decode(item["input_ids"][item["labels"] >= 0])
        print(f"{name}: {len(d)} samples; sample 0: {len(item['input_ids'])} tokens, images "
              f"{tuple(item['image'].shape)}, trained on {answer!r}")
    if missing:
        raise SystemExit("frames missing: copy navila_mix again")


# --- main --------------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mix", type=Path, required=True, help="navila_mix folder (deictic/, rxr_replay.json, rxr/)")
    ap.add_argument("--rxr-records", type=int, default=2000, help="RxR replay records (whole trajectories)")
    ap.add_argument("--rxr-seed", type=int, default=0)
    ap.add_argument("--attention", choices=("auto", "flash", "sdpa"), default="auto")
    ap.add_argument("--check", action="store_true", help="check data and attention swap, then exit")
    ours, navila_args = ap.parse_known_args()

    def navila_arg(name, default=None):
        return navila_args[navila_args.index(name) + 1] if name in navila_args else default

    out_dir = Path(navila_arg("--output_dir", "runs/check-lora"))
    if ours.check:
        check(ours.mix.resolve(), navila_arg("--model_name_or_path"), out_dir, ours.rxr_records, ours.rxr_seed)
        return
    if "lora" not in out_dir.name.lower():
        sys.exit(f"--output_dir {out_dir}: the name must contain 'lora' (NaVILA loads adapters only from such paths)")

    mode = use_sdpa_if_needed(ours.attention)
    counts = register(ours.mix.resolve(), out_dir, ours.rxr_records, ours.rxr_seed)
    if int(os.environ.get("RANK", "0")) == 0:
        print(f"[navila_lora] attention {mode}; records {counts}; mixture {navila_arg('--data_mixture')}", flush=True)

    from unittest import mock

    from llava.train.train import train
    from llava.train.train_mem import __iter__, __len__
    from llava.train.transformer_normalize_monkey_patch import patched_normalize

    sys.argv = [sys.argv[0]] + navila_args
    with (
        mock.patch("transformers.image_processing_utils.normalize", new=patched_normalize),
        mock.patch("accelerate.data_loader.BatchSamplerShard.__len__", new=__len__),
        mock.patch("accelerate.data_loader.BatchSamplerShard.__iter__", new=__iter__),
    ):
        train()


if __name__ == "__main__":
    main()
