# Data

Nothing in `data/` is committed except this file and `schema/` (the versioned episode format).
Datasets, bags and checkpoints live outside git: HF Hub, Drive, or the training servers.

Recorded sessions (`make collect`, [docs/collection_protocol.md](../docs/collection_protocol.md)) land in
`$DATA_DIR/sessions/session_<date>_<time>/`: one folder per episode (`ep_001/`: mcap bag + `episode.json`
with the instruction), `discarded/`, and `configs/` (copy of configs/ at the time). `make preview` shows
what is inside.

Flow: raw rosbag2 (mcap, never edited) → canonical episodes (`data/schema`) → NaVILA training format.
DeePoint augmentation converts into the same canonical format.

| What | Where | How to fetch |
|---|---|---|
| Raw bags | TBD | TBD |
| Canonical episodes | TBD | TBD |
| DeePoint | TBD | TBD |
| NaVILA weights (`navila-llama3-8b-8f`) | workstation, `NAVILA_CHECKPOINTS_DIR` | Hugging Face |
