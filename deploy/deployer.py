"""claudino deployer: lets CI deploy the relay on hermes over the tailnet.

POST /deploy?sha=<commit> fast-forwards ~/claude-relay to origin/main, checks that it is
<commit>, then runs scripts/deploy.sh (install, test, restart, verify, roll back on failure).
GET /health returns the deployed commit. It listens only on hermes' Tailscale address and
runs one deploy at a time; the only thing it can do is deploy what is on origin/main.
"""

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(os.environ.get("CLAUDINO_REPO", Path.home() / "claude-relay"))
HOST = os.environ.get("DEPLOYER_HOST", "100.99.99.71")
PORT = int(os.environ.get("DEPLOYER_PORT", "8765"))

lock = threading.Lock()


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=120)


def deploy(sha: str) -> tuple[int, str]:
    if not lock.acquire(blocking=False):
        return 409, "another deploy is running"
    try:
        fetch = git("fetch", "--quiet", "origin", "main")
        if fetch.returncode:
            return 500, "git fetch failed: " + fetch.stderr
        target = git("rev-parse", "origin/main").stdout.strip()
        if sha and not target.startswith(sha):
            return 409, f"origin/main is {target}, not {sha}"
        previous = git("rev-parse", "HEAD").stdout.strip()
        merge = git("merge", "--ff-only", "--quiet", "origin/main")
        if merge.returncode:
            return 500, "fast-forward failed (local changes on hermes?): " + merge.stderr
        run = subprocess.run([str(REPO / "scripts/deploy.sh"), previous], cwd=REPO,
                             capture_output=True, text=True, timeout=600)
        return (200 if run.returncode == 0 else 500), run.stdout[-4000:] + run.stderr[-2000:]
    finally:
        lock.release()


class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/deploy":
            return self.reply(404, "not found")
        self.reply(*deploy(parse_qs(url.query).get("sha", [""])[0]))

    def do_GET(self) -> None:
        if self.path != "/health":
            return self.reply(404, "not found")
        self.reply(200, git("rev-parse", "HEAD").stdout)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
