"""Standalone launcher for an always-on deployment (local machine or VM).

Credentials come from the environment and are never stored in this file:

    MONITOR_ACCESS_PASSWORD   required, at least 8 characters
    MONITOR_ACCESS_USERNAME   optional, defaults to "monitor"

Optional:

    PORT           listen port, defaults to 8812
    MONITOR_DB     SQLite path, defaults to <repo>/data/monitor.db
    MONITOR_CLOUD  1 (default here) enables the access-control middleware
    GATEWAY_COOKIE 1 additionally enables the cookie login adapter, needed
                   only behind a gateway that strips the Authorization header
                   (WorkBuddy Sites). Requires HTTPS, see sites_auth.py.

Usage:
    python deploy/windows/run_server.py
"""

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BASE))

os.environ.setdefault("MONITOR_CLOUD", "1")
os.environ.setdefault("MONITOR_DB", str(ROOT / "data" / "monitor.db"))

MIN_PASSWORD = 8


def main():
    password = os.environ.get("MONITOR_ACCESS_PASSWORD", "")

    if len(password) < MIN_PASSWORD:
        print(
            "MONITOR_ACCESS_PASSWORD is required and must be at least "
            f"{MIN_PASSWORD} characters.",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    from monitor.app import app

    if os.environ.get("GATEWAY_COOKIE") == "1":
        from sites_auth import CookieAuth

        app.add_middleware(CookieAuth)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8812")),
        log_level="info",
        proxy_headers=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
