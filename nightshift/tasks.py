"""Task file (`*.tasks.toml`) written in a planning session with the maintainer."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

TIERS = ("green", "yellow", "red")
REVIEWS = ("technical", "joint")  # joint = Claude + Codex (docs, generated UI, images)


class TaskFileError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    tier: str
    review: str
    paths: list[str]
    depends_on: list[str]
    no_tests: bool
    body: str


# IDs become branch names, file names and command arguments: keep them plain.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _task(raw: dict) -> Task:
    for key in ("id", "title", "tier", "review", "paths", "body"):
        if key not in raw:
            raise TaskFileError(f"task is missing {key!r}: {raw.get('id', '?')}")
    if not isinstance(raw["id"], str) or not _ID.match(raw["id"]):
        raise TaskFileError(f"invalid task id {raw['id']!r}: use letters, digits, '-' and '_' only")
    if raw["tier"] not in TIERS:
        raise TaskFileError(f"{raw['id']}: tier must be one of {TIERS}")
    if raw["review"] not in REVIEWS:
        raise TaskFileError(f"{raw['id']}: review must be one of {REVIEWS}")
    if not raw["paths"]:
        raise TaskFileError(f"{raw['id']}: paths must not be empty")
    return Task(
        id=raw["id"],
        title=raw["title"],
        tier=raw["tier"],
        review=raw["review"],
        paths=list(raw["paths"]),
        depends_on=list(raw.get("depends_on", [])),
        no_tests=bool(raw.get("no_tests", False)),
        body=raw["body"].strip(),
    )


def load_tasks(path: Path) -> tuple[str, list[Task]]:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    tasks = [_task(t) for t in raw.get("task", [])]
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise TaskFileError("duplicate task id")
    for t in tasks:
        unknown = set(t.depends_on) - set(ids)
        if unknown:
            raise TaskFileError(f"{t.id}: depends on unknown task(s) {sorted(unknown)}")
    return raw.get("plan", ""), tasks
