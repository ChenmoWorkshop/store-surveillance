"""Start the API and the tunnel as detached processes.

Useful for the first launch from a terminal: both children get
DETACHED_PROCESS so they survive after the parent shell exits. Windows
logon startup uses launch.vbs instead.
"""

import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
LOGS = ROOT / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

FLAGS = (
    getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)

ENV = dict(
    os.environ,
    PORT=os.environ.get("PORT", "8812"),
    MONITOR_CLOUD="1",
    MONITOR_DB=str(ROOT / "data" / "monitor.db"),
)


def spawn(script, log_name):
    log = open(LOGS / log_name, "a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(BASE / script)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=FLAGS,
        close_fds=True,
        env=ENV,
    )
    print(f"started {script} pid={process.pid}")


if __name__ == "__main__":
    if len(os.environ.get("MONITOR_ACCESS_PASSWORD", "")) < 8:
        print("MONITOR_ACCESS_PASSWORD is required (>= 8 chars).", file=sys.stderr)
        sys.exit(2)
    spawn("run_server.py", "server.log")
    spawn("tunnel_watch.py", "tunnel.log")
