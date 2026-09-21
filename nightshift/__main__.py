"""nightshift run | status | mode MODE | show TASK | answer TASK retry|skip | reset-breaker

The maintainer controls the night shift from Windows with, for example:
    wsl -d privasheet-dev -u agent -- nightshift answer SPK-01 retry --score 4
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path

from nightshift import human
from nightshift.config import MODES, load_config
from nightshift.dashboard import make_server, status
from nightshift.store import Store
from nightshift.supervisor import Supervisor

DEFAULT_CONFIG = os.environ.get("NIGHTSHIFT_CONFIG", "~/nightshift/config.toml")


def _run(config_path: Path, max_ticks: int | None) -> int:
    cfg = load_config(config_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(cfg.state_dir / "nightshift.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("nightshift is already running")
        return 0

    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())

    server = make_server(cfg.state_dir, host="127.0.0.1", port=cfg.port)
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    print(f"nightshift running; dashboard http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        Supervisor(config_path).run_forever(stop, max_ticks=max_ticks)
    finally:
        server.shutdown()
        server.server_close()
        lock.close()
    return 0


def _set_mode(config_path: Path, mode: str) -> int:
    if mode not in MODES:
        print(f"mode must be one of: {', '.join(MODES)}")
        return 2
    text = config_path.read_text(encoding="utf-8")
    new, count = re.subn(r'^mode\s*=\s*"[^"]*"', f'mode = "{mode}"', text, count=1, flags=re.M)
    if count == 0:
        new = f'mode = "{mode}"\n' + text
    config_path.write_text(new, encoding="utf-8")
    load_config(config_path)  # still valid
    print(f"mode = {mode} (applies before the next step)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nightshift")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run the loop and the dashboard (one instance)")
    p.add_argument("--max-ticks", type=int, default=None, help=argparse.SUPPRESS)
    sub.add_parser("status", help="print the dashboard data as JSON")
    p = sub.add_parser("mode", help="set AUTOPILOT mode")
    p.add_argument("mode")
    p = sub.add_parser("show", help="print an open needs-human file")
    p.add_argument("task_id")
    p = sub.add_parser("answer", help="answer a needs-human escalation")
    p.add_argument("task_id")
    p.add_argument("decision", choices=["retry", "skip"])
    p.add_argument("--score", type=int, choices=range(1, 6), default=None)
    sub.add_parser("reset-breaker", help="close the circuit breaker after fixing the cause")
    for sp in sub.choices.values():
        sp.add_argument("--config", default=DEFAULT_CONFIG)

    args = parser.parse_args(argv)
    config_path = Path(os.path.expanduser(args.config))

    if args.command == "run":
        return _run(config_path, args.max_ticks)
    if args.command == "mode":
        return _set_mode(config_path, args.mode)

    cfg = load_config(config_path)
    inbox = cfg.state_dir / "needs-human"
    if args.command == "status":
        print(json.dumps(status(cfg.state_dir), indent=2))
        return 0
    if args.command == "show":
        path = inbox / f"{args.task_id}.md"
        if not path.exists():
            print(f"no open needs-human file for {args.task_id}")
            return 1
        print(path.read_text(encoding="utf-8"))
        return 0
    if args.command == "answer":
        if not human.answer(inbox, args.task_id, args.decision, args.score):
            print(f"no open needs-human file for {args.task_id}")
            return 1
        print(f"{args.task_id}: {args.decision} recorded; applied on the next loop")
        return 0

    store = Store(cfg.state_dir / "nightshift.db")
    store.set_state("breaker_open", "0")
    store.set_state("breaker_reason", None)
    store.set_state("consecutive_failures", "0")
    print("circuit breaker reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
