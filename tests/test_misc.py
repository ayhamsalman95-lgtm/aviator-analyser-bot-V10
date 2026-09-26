import json
import os
import re
import unittest

from tests.helpers import ROOT, TempProject
from aviator.config import load_config
from aviator.netlog import RotatingJsonlLog, redact, safe_url


class DependencyTests(unittest.TestCase):
    def test_requirements_declare_sfs2x(self):
        req = (ROOT / "requirements.txt").read_text()
        self.assertRegex(req, r"(?m)^sfs2x-py==")
        self.assertRegex(req, r"(?m)^python-telegram-bot")
        self.assertRegex(req, r"(?m)^playwright")

    def test_import_name_used_is_sfs2x(self):
        src = (ROOT / "aviator" / "sfs_codec.py").read_text()
        self.assertIn("from sfs2x import decode_s2c_packet", src)

    def test_core_imports_without_optional_deps(self):
        import aviator.collector  # noqa: F401  (playwright imported lazily)
        import aviator.tracker  # noqa: F401

    def test_no_legacy_modules_left(self):
        for name in ("seeds.py", "store.py", "fairness.py", "predictor.py", "patch_fairness.py"):
            self.assertFalse((ROOT / name).exists(), name)

    def test_installer_does_not_reference_missing_script(self):
        for bat in ROOT.glob("*.bat"):
            text = bat.read_text(encoding="utf-8", errors="ignore")
            for script in re.findall(r"python(?:\.exe)?\s+([\w\\/.-]+\.py)", text, re.I):
                self.assertTrue((ROOT / script.replace("\\", "/")).exists(), f"{bat.name} -> {script}")


class SecurityTests(unittest.TestCase):
    def test_token_from_env_only(self):
        p = TempProject()
        try:
            (p.dir / "config.json").write_text(json.dumps({"bot_token": "123:FROMCONFIG"}))
            cfg = load_config(root=p.dir)
            old = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            try:
                self.assertIsNone(cfg.telegram_token())
                os.environ["TELEGRAM_BOT_TOKEN"] = "YOUR_TELEGRAM_BOT_TOKEN"
                self.assertIsNone(cfg.telegram_token())
                os.environ["TELEGRAM_BOT_TOKEN"] = "42:abc"
                self.assertEqual(cfg.telegram_token(), "42:abc")
            finally:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                if old is not None:
                    os.environ["TELEGRAM_BOT_TOKEN"] = old
        finally:
            p.cleanup()

    def test_gitignore_excludes_sensitive_and_runtime(self):
        gi = (ROOT / ".gitignore").read_text()
        for pat in ("chrome_profile/", ".env", "data/", "logs/", "*.sqlite3", "rounds.json", "diagnostics.jsonl"):
            self.assertIn(pat, gi)

    def test_repo_config_has_no_token(self):
        self.assertNotIn("bot_token", json.loads((ROOT / "config.json").read_text()))

    def test_redaction(self):
        self.assertEqual(redact({"Cookie": "x", "a": {"token": "y", "b": 1}}),
                         {"Cookie": "[redacted]", "a": {"token": "[redacted]", "b": 1}})
        self.assertNotIn("SECRET", safe_url("https://h/p?token=SECRET&x=1"))


class NetlogTests(unittest.TestCase):
    def test_rotation(self):
        p = TempProject()
        try:
            log = RotatingJsonlLog(p.dir / "n.jsonl", max_bytes=2000, backups=2)
            for i in range(200):
                log.write({"kind": "x", "i": i, "pad": "p" * 50})
            files = sorted(x.name for x in p.dir.glob("n.jsonl*"))
            self.assertEqual(files, ["n.jsonl", "n.jsonl.1", "n.jsonl.2"])
            self.assertLessEqual((p.dir / "n.jsonl").stat().st_size, 2000)
        finally:
            p.cleanup()


if __name__ == "__main__":
    unittest.main()
