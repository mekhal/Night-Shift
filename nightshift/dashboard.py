"""Read-only local dashboard: GET only, loopback Host only, no document data, text-only rendering."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from nightshift.human import list_open
from nightshift.store import Store

STATE_KEYS = (
    "mode", "heartbeat", "last_action", "current_task", "step_started_at",
    "breaker_open", "breaker_reason", "consecutive_failures", "config_error", "tasks_error", "last_error",
)

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

MAX_FILE_BYTES = 2 * 1024 * 1024

_NAME = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
_RUN_ID = re.compile(r"^[A-Za-z0-9-]+$")
_FILE_NAME = re.compile(rf"^{_NAME}$")
_HUMAN_FILE = re.compile(rf"^/needs-human/({_NAME}\.md)$")
_RUN_FILE = re.compile(rf"^/runs/([A-Za-z0-9-]+)/({_NAME})$")


def _run_files(runs_root: Path, run_id: str) -> list[str]:
    """Files the /runs route can serve; anything odd (bad id, escape, I/O error) lists nothing."""
    if not _RUN_ID.match(run_id):
        return []
    try:
        run_dir = (runs_root / run_id).resolve()
        if run_dir.parent != runs_root.resolve() or not run_dir.is_dir():
            return []
        return sorted(p.name for p in run_dir.iterdir() if p.is_file() and _FILE_NAME.match(p.name))
    except OSError:
        return []


def _read_capped(path: Path) -> bytes:
    size = path.stat().st_size
    if size <= MAX_FILE_BYTES:
        return path.read_bytes()
    with path.open("rb") as fh:
        fh.seek(size - MAX_FILE_BYTES)
        tail = fh.read()
    return f"[truncated: last {MAX_FILE_BYTES} of {size} bytes]\n".encode() + tail


def status(state_dir: Path) -> dict:
    store = Store(state_dir / "nightshift.db")
    try:
        runs_root = state_dir / "runs"
        runs = store.recent_runs(30)
        for run in runs:
            run["files"] = _run_files(runs_root, run["run_id"])
        return {
            "server_time": datetime.now(UTC).isoformat(timespec="seconds"),
            "state_dir": str(state_dir),
            "state": {key: store.get_state(key) for key in STATE_KEYS},
            "limits": store.usage_limits(),
            "tasks": store.list_tasks(),
            "runs": runs,
            "needs_human": list_open(state_dir / "needs-human"),
        }
    finally:
        store.conn.close()


def make_server(state_dir: Path, host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer:
    state_dir = state_dir.resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "nightshift"
        sys_version = ""

        def log_message(self, format, *args):  # keep stdout quiet
            pass

        def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self) -> bool:
            port_now = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port_now}", f"localhost:{port_now}")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden")
            path = self.path.split("?", 1)[0]
            if path in STATIC:
                name, content_type = STATIC[path]
                body = resources.files("nightshift.static").joinpath(name).read_bytes()
                return self._send(200, body, content_type)
            if path == "/api/status":
                return self._send(200, json.dumps(status(state_dir)).encode(), "application/json")
            if m := _HUMAN_FILE.match(path):
                inbox = state_dir / "needs-human"
                target = (inbox / m.group(1)).resolve()
                if target.parent == inbox and target.is_file():
                    return self._send(200, _read_capped(target))
            if m := _RUN_FILE.match(path):
                runs_root = state_dir / "runs"
                target = (runs_root / m.group(1) / m.group(2)).resolve()
                if target.parent.parent == runs_root and target.is_file():
                    return self._send(200, _read_capped(target))
            return self._send(404, b"not found")

        do_HEAD = do_GET

        def _not_allowed(self):
            self._send(405, b"read-only")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    server = ThreadingHTTPServer((host, port), Handler)
    server.state_dir = state_dir
    return server
