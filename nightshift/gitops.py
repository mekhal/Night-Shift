"""Git operations: one worktree per task attempt, merge --no-ff into the base branch."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


# Tool caches created by tests and linters; excluded for every worktree via the shared info/exclude.
CACHE_EXCLUDES = ["__pycache__/", "*.pyc", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/", ".venv/"]


def ensure_clean_base(repo: Path, base: str) -> None:
    """Base branch checked out with no tracked changes. Untracked files (caches) are ignored."""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != base:
        raise GitError(f"main checkout must be on {base}, found {branch}")
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise GitError(f"main checkout must be clean on {base}")


def _ensure_cache_excludes(repo: Path) -> None:
    common = Path(_git(repo, "rev-parse", "--git-common-dir").stdout.strip())
    exclude = (common if common.is_absolute() else repo / common) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [p for p in CACHE_EXCLUDES if p not in existing]
    if missing:
        with exclude.open("a") as fh:
            fh.write("\n# nightshift: tool caches\n" + "\n".join(missing) + "\n")


def create_worktree(repo: Path, root: Path, task_id: str, attempt: int, base: str) -> Worktree:
    _ensure_cache_excludes(repo)
    path = root / task_id
    branch = f"task/{task_id}-a{attempt}"
    if path.exists():
        _git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    _git(repo, "worktree", "prune")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        _git(repo, "branch", "-D", branch)
    root.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    return Worktree(path=path, branch=branch)


def remove_worktree(repo: Path, path: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    _git(repo, "worktree", "prune")


def commit_all(path: Path, message: str) -> bool:
    _git(path, "add", "-A")
    if _git(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(path, "commit", "-q", "-m", message)
    return True


def diff_against(path: Path, base: str) -> str:
    return _git(path, "diff", "--no-renames", "--no-color", f"{base}...HEAD").stdout


def merge(repo: Path, branch: str, message: str) -> str:
    result = _git(repo, "merge", "--no-ff", "-m", message, branch, check=False)
    if result.returncode != 0:
        _git(repo, "merge", "--abort", check=False)
        raise GitError(f"merge of {branch} failed: {result.stdout.strip()} {result.stderr.strip()}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def revert_merge(repo: Path, sha: str) -> None:
    _git(repo, "revert", "-m", "1", "--no-edit", sha)
