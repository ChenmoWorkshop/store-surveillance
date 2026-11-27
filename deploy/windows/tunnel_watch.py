"""Launch a cloudflared quick tunnel and keep it alive.

Writes the current public URL to public-url.txt whenever the tunnel is
(re)created, so a changing trycloudflare hostname is always discoverable
locally. Restarts cloudflared if it exits.

Quick tunnels get a new random hostname on every restart. For a stable
address create a named tunnel in Cloudflare Zero Trust and point your own
domain at it instead.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]

LOG = ROOT / "logs" / "tunnel.log"
URL_FILE = ROOT / "public-url.txt"
PORT = os.environ.get("PORT", "8812")
TARGET = f"http://127.0.0.1:{PORT}"
PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

CANDIDATES = [
    ROOT / "bin",
    Path.home() / "AppData/Local/Microsoft/WinGet/Packages",
]


def find_cloudflared():
    local = ROOT / "bin" / "cloudflared.exe"
    if local.exists():
        return str(local)
    found = shutil.which("cloudflared")
    if found:
        return found
    for base in CANDIDATES:
        if not base.exists():
            continue
        for exe in base.rglob("cloudflared.exe"):
            return str(exe)
    return None


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")


def main():
    exe = find_cloudflared()
    if not exe:
        log("cloudflared.exe not found; tunnel not started")
        return 1
    log(f"using cloudflared: {exe}")
    delay = 10
    while True:
        command = [
            exe,
            "tunnel",
            "--url",
            TARGET,
            "--no-autoupdate",
            # Some hosts resolve the edge SRV records over a broken IPv6
            # DNS server; forcing the us region over IPv4/HTTP2 avoids it.
            "--region",
            "us",
            "--edge-ip-version",
            "4",
            "--protocol",
            "http2",
        ]
        log("starting: " + " ".join(command))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.stdout is not None:
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                log(line)
                match = PATTERN.search(line)
                if match:
                    url = match.group(0)
                    delay = 10
                    URL_FILE.write_text(
                        f"{url}\nupdated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                        encoding="utf-8",
                    )
                    log(f"public url: {url}")
        code = process.wait()
        log(f"cloudflared exited with {code}; restart in {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 300)


if __name__ == "__main__":
    sys.exit(main())
