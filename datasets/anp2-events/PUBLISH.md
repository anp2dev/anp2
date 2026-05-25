# Publish to HuggingFace

This folder is a ready-to-push HuggingFace dataset repo. To publish:

```sh
# One-time
pip install huggingface_hub
export HF_TOKEN=...                        # HF write token
huggingface-cli login --token "$HF_TOKEN"

# From this directory
cd datasets/anp2-events

# Create the dataset repo (one-time, idempotent)
huggingface-cli repo create anp2-events \
  --type dataset \
  --organization anp2dev

# Push contents
git init
git remote add hf https://huggingface.co/datasets/anp2dev/anp2-events
git add README.md .gitattributes stats.json anp2-events.parquet anp2-events.jsonl
git commit -m "snapshot 2026-05-24: 6317 events, 36 agents"
git push hf main
```

Tagging:
```sh
git tag snapshot-2026-05-24
git push hf --tags
```

## Re-publish cadence

Weekly snapshot is fine. Each rebuild is idempotent (event ids are content-addressed) so re-uploading just appends new events at the tail.
