"""Dependency / environment check. Exit code 1 if a required dependency is missing."""
import importlib
import json
import sys

import _bootstrap  # noqa: F401

from aviator.config import load_config
from aviator.sfs_codec import dependency_report

REQUIRED = {"telegram": "python-telegram-bot", "playwright": "playwright", "sfs2x": "sfs2x-py"}


def main() -> int:
    ok = True
    out = {"python": sys.version.split()[0]}
    if sys.version_info < (3, 10):
        ok = False
        out["python_error"] = "Python 3.10+ required (sfs2x-py)"
    for mod, pkg in REQUIRED.items():
        try:
            importlib.import_module(mod)
            out[pkg] = "ok"
        except Exception as exc:
            out[pkg] = f"MISSING ({type(exc).__name__}: {exc})"
            ok = False
    out["sfs2x_api"] = dependency_report()
    if out["sfs2x_api"].get("available") and not (out["sfs2x_api"].get("has_decode_s2c_packet")
                                                 and out["sfs2x_api"].get("has_parse_s2c_command")):
        ok = False
    out["telegram_token_set"] = bool(load_config().telegram_token())
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
