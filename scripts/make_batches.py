"""Generate any due TXT batch files (every `batch_size` valid rounds)."""
import _bootstrap  # noqa: F401

from aviator.batches import generate_due_batches
from aviator.config import load_config
from aviator.db import Store

if __name__ == "__main__":
    created = generate_due_batches(Store.from_config(load_config()))
    print("created:", [p.name for p in created] or "none")
