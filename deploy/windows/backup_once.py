"""Copy the SQLite database into data/backups, keeping the newest 14 files."""

import shutil
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]

DB = ROOT / "data" / "monitor.db"
BACKUPS = ROOT / "data" / "backups"
KEEP = 14


def main():
    if not DB.exists():
        return
    BACKUPS.mkdir(parents=True, exist_ok=True)
    target = BACKUPS / f"monitor-{time.strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(DB, target)
    files = sorted(BACKUPS.glob("monitor-*.db"))
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
