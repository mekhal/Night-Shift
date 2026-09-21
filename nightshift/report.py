"""Daily markdown report: one row per run."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def append_report(directory: Path, day: str, run_id: str, task_id: str, outcome: str, detail: str,
                  seconds: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.md"
    if not path.exists():
        path.write_text(
            f"# Night shift report {day}\n\n| run | time (UTC) | task | outcome | minutes | detail |\n"
            "|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    clean = " ".join(detail.split()).replace("|", "\\|")[:300]
    stamp = datetime.now(UTC).strftime("%H:%M")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"| {run_id} | {stamp} | {task_id} | {outcome} | {seconds / 60:.1f} | {clean} |\n")
