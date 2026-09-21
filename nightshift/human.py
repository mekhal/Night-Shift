"""needs-human files: the orchestrator writes them; the maintainer answers by editing `decision:`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_DECISIONS = {"a": "retry", "retry": "retry", "b": "skip", "skip": "skip"}

TEMPLATE = """# 🔶 needs-human: {task_id}

type: {kind}
task: {task_id}
branch: {branch}
run: {run_id}
created: {created}

## Reason

{reason}

## Options

A) retry — the task goes back to the queue with attempts reset (fix the cause first, e.g. the task text)
B) skip — the task is marked skipped

## Answer (fill in and save)

decision:
score:
"""


@dataclass(frozen=True)
class Answer:
    task_id: str
    decision: str
    score: int | None


def write_escalation(directory: Path, task_id: str, kind: str, reason: str, branch: str, run_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.md"
    path.write_text(
        TEMPLATE.format(task_id=task_id, kind=kind, reason=reason.strip(), branch=branch, run_id=run_id,
                        created=datetime.now(UTC).isoformat(timespec="seconds")),
        encoding="utf-8",
    )
    return path


def _field(text: str, name: str) -> str:
    m = re.search(rf"^{name}:[ \t]*(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def answer(directory: Path, task_id: str, decision: str, score: int | None) -> bool:
    """Fill in the answer of an open escalation. Returns False if there is no open file for the task."""
    path = directory / f"{task_id}.md"
    if decision not in ("retry", "skip") or not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^decision:.*$", f"decision: {decision}", text, count=1, flags=re.M)
    text = re.sub(r"^score:.*$", f"score: {score if score is not None else ''}", text, count=1, flags=re.M)
    path.write_text(text, encoding="utf-8")
    return True


def list_open(directory: Path) -> list[dict]:
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        out.append({"task_id": path.stem, "type": _field(text, "type"), "run": _field(text, "run"),
                    "created": _field(text, "created"), "file": path.name})
    return out


def collect_answers(directory: Path) -> list[Answer]:
    """Answered files are moved to answered/; files without a valid decision are left in place."""
    if not directory.exists():
        return []
    answers = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        decision = _DECISIONS.get(_field(text, "decision").lower())
        if decision is None:
            continue
        score_text = _field(text, "score")
        score = int(score_text) if score_text.isdigit() else None
        done = directory / "answered"
        done.mkdir(exist_ok=True)
        path.rename(done / f"{path.stem}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.md")
        answers.append(Answer(path.stem, decision, score))
    return answers
