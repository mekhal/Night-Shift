"""Deterministic gates: rules over the diff, plus configured commands (pytest, ruff, ...)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from nightshift.agents import run
from nightshift.config import Gate
from nightshift.tasks import Task


@dataclass(frozen=True)
class GateFailure:
    gate: str
    detail: str


@dataclass
class FileDiff:
    path: str
    kind: str = "modified"  # new | deleted | modified
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def parse_diff(text: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    in_header = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = FileDiff(path=line.split(" b/", 1)[-1])
            files[current.path] = current
            in_header = True
            continue
        if current is None:
            continue
        if in_header:
            if line.startswith("new file mode"):
                current.kind = "new"
            elif line.startswith("deleted file mode"):
                current.kind = "deleted"
            elif line.startswith("@@"):
                in_header = False
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("+"):
            current.added.append(line[1:])
        elif line.startswith("-"):
            current.removed.append(line[1:])
    return files


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, p) for p in patterns)


def _is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix == ".py" and (p.name.startswith("test_") or p.stem.endswith("_test") or "tests" in p.parts)


_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)")
_SKIP = re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\(|unittest\.skip")
_SECRETS = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_\w{22,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AIza[0-9A-Za-z_-]{35}"
)


def check_diff(
    files: dict[str, FileDiff],
    task: Task,
    red_paths: list[str],
    network_modules: list[str],
    max_lines: int,
    network_allowed_paths: list[str] = (),
) -> list[GateFailure]:
    out: list[GateFailure] = []
    if not files:
        return [GateFailure("no-changes", "the diff is empty")]

    total = sum(len(f.added) + len(f.removed) for f in files.values())
    if total > max_lines:
        out.append(GateFailure("size", f"{total} changed lines > {max_lines}"))

    outside = sorted(p for p in files if not _matches(p, task.paths))
    if outside:
        out.append(GateFailure("scope", "outside allowed paths: " + ", ".join(outside)))

    # A task unlocks a human-only path only by naming it literally: the task file is itself human-only, so that
    # spelling is a maintainer decision. A wildcard in the task's paths never unlocks one.
    red = sorted(p for p in files if _matches(p, red_paths) and p not in task.paths)
    if red:
        out.append(GateFailure("red-path", "human-only paths changed: " + ", ".join(red)))

    if not task.no_tests and not any(_is_test_file(p) and f.kind != "deleted" for p, f in files.items()):
        out.append(GateFailure("new-tests", "no test file added or changed"))

    weakened = []
    added_defs = {m.group(1) for f in files.values() for ln in f.added if (m := _TEST_DEF.match(ln))}
    for p, f in files.items():
        if f.kind == "deleted" and _is_test_file(p):
            weakened.append(f"deleted {p}")
        for ln in f.removed:
            if (m := _TEST_DEF.match(ln)) and m.group(1) not in added_defs:
                weakened.append(f"removed {m.group(1)} in {p}")
        if any(_SKIP.search(ln) for ln in f.added):
            weakened.append(f"skip/xfail added in {p}")
    if weakened:
        out.append(GateFailure("tests-weakened", "; ".join(weakened)))

    mods = "|".join(re.escape(m) for m in network_modules)
    net = re.compile(rf"^\s*(?:import\s+(?:{mods})(?:[\s,.]|$)|from\s+(?:{mods})[\s.])")
    net_hits = sorted({
        p for p, f in files.items()
        if p.endswith(".py") and not _matches(p, list(network_allowed_paths)) and any(net.match(ln) for ln in f.added)
    })
    if net_hits:
        out.append(GateFailure("network", "network library imported in: " + ", ".join(net_hits)))

    secret_hits = sorted({p for p, f in files.items() if any(_SECRETS.search(ln) for ln in f.added)})
    if secret_hits:
        out.append(GateFailure("secrets", "possible secret added in: " + ", ".join(secret_hits)))
    return out


def run_command_gates(gates: list[Gate], cwd: Path, timeout: float) -> list[GateFailure]:
    out = []
    for gate in gates:
        result = run(gate.cmd, cwd=cwd, timeout=timeout)
        if result.timed_out:
            out.append(GateFailure(gate.name, f"timed out after {timeout:.0f} s"))
        elif result.code != 0:
            out.append(GateFailure(gate.name, result.text.strip()[-4000:]))
    return out
