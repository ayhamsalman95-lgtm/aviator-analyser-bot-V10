"""Write reports/report_<stamp>.md and .json (stats + walk-forward + logged predictions)."""
import _bootstrap  # noqa: F401

from aviator.config import load_config
from aviator.db import Store
from aviator.reports import write_report

if __name__ == "__main__":
    cfg = load_config()
    j, m = write_report(Store.from_config(cfg), cfg)
    print(f"report: {m}\njson:   {j}")
