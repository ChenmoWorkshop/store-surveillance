"""Restore a JSON backup produced by tools/backup_cloud.py.

Without --replace the target database must be empty (only the default rules row),
which is the normal case right after a fresh deployment. With --replace the
existing data is wiped first, so --yes is also required.

Example:
  python tools/restore_cloud.py backups/cloud-20261122-2332.json --replace --yes
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.environ["MONITOR_BASE_URL"].rstrip("/")
AUTH = "Basic " + base64.b64encode(
    f"{os.environ.get('MONITOR_ACCESS_USERNAME', 'monitor')}:{os.environ['MONITOR_ACCESS_PASSWORD']}".encode()
).decode()


def main():
    parser = argparse.ArgumentParser(description="恢复监控台的全量备份")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--replace", action="store_true", help="覆盖线上现有数据")
    parser.add_argument("--yes", action="store_true", help="确认覆盖，不会再次询问")
    args = parser.parse_args()
    if args.replace and not args.yes:
        sys.exit("覆盖恢复需要同时加 --yes")

    payload = json.loads(args.backup.read_text(encoding="utf-8"))
    payload["mode"] = "replace" if args.replace else "empty"
    if args.replace:
        payload["confirm"] = "replace"
    request = Request(BASE + "/api/v1/admin/restore", method="POST",
                      data=json.dumps(payload).encode(),
                      headers={"Authorization": AUTH, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            result = json.load(response)
    except HTTPError as error:
        sys.exit(f"恢复失败 HTTP {error.code}: {error.read().decode(errors='replace')}")
    print("restored", result["restored"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
