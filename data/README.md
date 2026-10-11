# Data

Nothing in `data/` is committed except this file and `schema/` (the versioned episode format).
Datasets, bags and checkpoints live outside git: HF Hub, Drive, or the training servers.

Flow: raw rosbag2 (mcap, never edited) → canonical episodes (`data/schema`) → NaVILA training format.
DeePoint augmentation converts into the same canonical format.

| What | Where | How to fetch |
|---|---|---|
| Raw bags | TBD | TBD |
| Canonical episodes | TBD | TBD |
| DeePoint | TBD | TBD |
| NaVILA weights (`navila-llama3-8b-8f`) | workstation, `NAVILA_CHECKPOINTS_DIR` | Hugging Face |
