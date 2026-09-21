"""Prompts sent to the implementer (Codex) and reviewers (Claude, Codex)."""

from __future__ import annotations

from fnmatch import fnmatch

from nightshift.config import DEFAULT_NETWORK_MODULES, DEFAULT_RED_PATHS, Gate
from nightshift.tasks import Task


def _gates_block(gates: list[Gate]) -> str:
    """The exact commands the work is judged by, so the implementer can run them itself."""
    if not gates:
        return ""
    listed = "".join(f"  - {g.name}: {' '.join(g.cmd)}\n" for g in gates)
    return (
        "\nDeterministic gates — run these exact commands in the worktree and make them pass before you finish.\n"
        "Work that leaves a gate failing is rejected however good the code is. A check-only gate usually has a\n"
        "fixing counterpart (e.g. `ruff check --fix .`, `ruff format .` for `ruff format --check .`); use it.\n"
        f"{listed}"
    )


def _task_block(task: Task) -> str:
    return (
        f"Task: {task.id} — {task.title}\n"
        f"Allowed paths (you may only create or change files matching these):\n"
        + "".join(f"  - {p}\n" for p in task.paths)
        + f"\n{task.body}\n"
    )


def _named_literally(paths: list[str], patterns: list[str]) -> list[str]:
    """The task's own path entries that match `patterns`; a wildcard entry never names a file."""
    return [p for p in paths if any(fnmatch(p, pattern) for pattern in patterns)]


def _rules_block(task: Task, red_paths: list[str], network_modules: list[str],
                 network_allowed_paths: list[str]) -> str:
    """The rules the gates really apply to this task: a rule stricter than the gates only costs a refusal."""
    human_only = ", ".join(red_paths)
    unlocked = _named_literally(task.paths, red_paths)
    if unlocked:
        # The task file is human-only itself, so a file spelled out there is the human decision the gate asks for.
        named = ", ".join(unlocked)
        scope = (f"- Change only files inside the allowed paths. These paths are human-only: {human_only}. "
                 f"The task file spells out {named} in the allowed paths above, so for this task you may "
                 f"change {named} - and no other human-only file.")
    else:
        scope = f"- Change only files inside the allowed paths. Never touch {human_only}."

    network = f"- Do not add network access ({', '.join(network_modules)})."
    open_here = _named_literally(task.paths, network_allowed_paths)
    if open_here:
        network += f" The plan allows them in {', '.join(open_here)}, which this task may change."

    return f"""Rules:
- Work test-first: write failing tests for the acceptance criteria, then the code. Run the tests before you finish.
{scope}
- Do not delete, skip or xfail existing tests.
{network}
- Do not add dependencies. Do not add secrets, tokens or real personal data; use synthetic data only.
- Do not run git commit, git push or change branches. The orchestrator commits your work.
- If the task is ambiguous or cannot be done within these rules, stop and explain why instead of guessing.
"""


def implement_prompt(task: Task, gates: list[Gate] | None = None, red_paths: list[str] = DEFAULT_RED_PATHS,
                     network_modules: list[str] = DEFAULT_NETWORK_MODULES,
                     network_allowed_paths: list[str] = ()) -> str:
    rules = _rules_block(task, list(red_paths), list(network_modules), list(network_allowed_paths))
    return f"You are implementing one task in this repository.\n\n{_task_block(task)}\n{rules}{_gates_block(gates or [])}"


def fix_prompt(task: Task, problems: list[str], gates: list[Gate] | None = None,
               red_paths: list[str] = DEFAULT_RED_PATHS, network_modules: list[str] = DEFAULT_NETWORK_MODULES,
               network_allowed_paths: list[str] = ()) -> str:
    rules = _rules_block(task, list(red_paths), list(network_modules), list(network_allowed_paths))
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        f"Your previous change for this task was not accepted. Fix these problems in the working tree.\n\n"
        f"{_task_block(task)}\nProblems:\n{listed}\n\n{rules}{_gates_block(gates or [])}"
    )


def review_prompt(task: Task, diff: str, gates: str) -> str:
    return f"""You are an independent code reviewer. You may read files in this repository but must not change anything.

You may also run read-only shell commands to check a fact instead of guessing at it. The project virtualenv named
in the gate commands below holds the third-party packages this code calls, so the real API of an external library
can always be verified there (its signature, its accepted configuration keys, its behaviour). Never modify a file,
install a package or use the network. A finding that rests on what you recall about a library, or on not having
been able to check it, is not acceptable: verify it, or leave it out.

{_task_block(task)}
Deterministic gates: {gates}

Review the diff below against the task and its acceptance criteria. Check correctness, tests that really exercise
the behaviour, scope, security and privacy (no real personal data, no network access), and maintainability.

Choose a verdict:
- "approve": acceptance criteria are met and you found no high or medium issue.
- "changes": fixable issues; list each with a concrete fix.
- "escalate": a human must decide — criteria ambiguous or unmet in a way a fix cannot resolve, work beyond the task,
  unstated assumptions, changed validation or human-review thresholds, or you are uncertain.

Risk: "green" = routine; "yellow" = needs care but acceptable; "red" = must not merge without a human.

Answer with only this JSON object:
{{"verdict": "approve | changes | escalate", "risk": "green | yellow | red", "findings": [{{"severity": "high|medium|low", "file": "...", "line": 1, "issue": "...", "fix": "..."}}], "reason": "..."}}

Diff:
```diff
{diff}
```
"""
