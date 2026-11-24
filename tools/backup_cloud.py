"""Download a full JSON backup from the deployed monitor.

Credentials are read from MONITOR_BASE_URL / MONITOR_ACCESS_USERNAME /
MONITOR_ACCESS_PASSWORD, falling back to ~/.workbuddy/monitor-backup.json.
Read-only: one GET request. Keeps the newest 30 cloud-*.json files.
"""
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path.home() / ".workbuddy" / "monitor-backup.json"
KEEP = 30


def setting(name, default=None):
    value = os.environ.get(name)
    if value:
        return value
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8")).get(name, default)
    return default


def main():
    base = setting("MONITOR_BASE_URL")
    password = setting("MONITOR_ACCESS_PASSWORD")
    user = setting("MONITOR_ACCESS_USERNAME", "monitor")
    if not base or not password:
        sys.exit("缺少 MONITOR_BASE_URL 或 MONITOR_ACCESS_PASSWORD")
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    request = Request(base.rstrip("/") + "/api/v1/admin/export",
                      headers={"Authorization": auth, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError) as error:
        sys.exit(f"备份失败：{error}")
    folder = ROOT / "backups"
    folder.mkdir(exist_ok=True)
    path = folder / f"cloud-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    for old in sorted(folder.glob("cloud-*.json"))[:-KEEP]:
        old.unlink()
    counts = payload["counts"]
    print(f"saved {path.name} targets={counts['targets']} snapshots={counts['snapshots']} "
          f"jobs={counts['jobs']} events={counts['events']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
