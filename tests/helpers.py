import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from aviator.config import load_config  # noqa: E402
from aviator.db import Store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, dt=1.0):
        self.t += dt
        return self.t


class TempProject:
    def __init__(self, **overrides):
        self.dir = Path(tempfile.mkdtemp(prefix="aviator_test_"))
        self.cfg = load_config(path=self.dir / "none.json", root=self.dir, overrides=overrides)
        self.clock = Clock()
        self.store = self.new_store()

    def new_store(self):
        return Store(self.cfg.db_path, self.cfg.structured_dir, self.cfg.batches_dir,
                     batch_size=int(self.cfg["batch_size"]), clock=self.clock)

    def reopen(self):
        self.store.close()
        self.store = self.new_store()
        return self.store

    def cleanup(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def load_replay(name="sfs_replay.jsonl"):
    out = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out
