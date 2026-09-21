import subprocess

import pytest

from nightshift import gitops


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    sh(path, "init", "-q", "-b", "develop")
    sh(path, "config", "user.name", "test")
    sh(path, "config", "user.email", "test@localhost")
    (path / "app.py").write_text("x = 1\n")
    sh(path, "add", "-A")
    sh(path, "commit", "-q", "-m", "init")
    return path


def test_worktree_commit_diff_and_merge(repo, tmp_path):
    wt = gitops.create_worktree(repo, tmp_path / "wt", "T-1", attempt=1, base="develop")
    assert wt.branch == "task/T-1-a1"
    assert (wt.path / "app.py").exists()

    assert gitops.commit_all(wt.path, "empty") is False
    (wt.path / "app.py").write_text("x = 2\n")
    (wt.path / "new.py").write_text("y = 1\n")
    assert gitops.commit_all(wt.path, "T-1: change") is True

    diff = gitops.diff_against(wt.path, "develop")
    assert "+x = 2" in diff and "new.py" in diff

    sha = gitops.merge(repo, wt.branch, "Merge T-1\n\nTask: T-1")
    assert (repo / "new.py").read_text() == "y = 1\n"
    assert sh(repo, "log", "-1", "--format=%P", sha).split().__len__() == 2  # merge commit, not fast-forward
    assert "Task: T-1" in sh(repo, "log", "-1", "--format=%B")

    gitops.revert_merge(repo, sha)
    assert not (repo / "new.py").exists()

    gitops.remove_worktree(repo, wt.path)
    assert not wt.path.exists()
    assert "task/T-1-a1" in sh(repo, "branch", "--list", "task/T-1-a1")  # branch kept for inspection


def test_new_attempt_replaces_stale_worktree_directory(repo, tmp_path):
    first = gitops.create_worktree(repo, tmp_path / "wt", "T-2", attempt=1, base="develop")
    (first.path / "junk.txt").write_text("left over")

    second = gitops.create_worktree(repo, tmp_path / "wt", "T-2", attempt=2, base="develop")

    assert second.branch == "task/T-2-a2"
    assert not (second.path / "junk.txt").exists()


def test_main_checkout_must_be_clean_on_base(repo):
    gitops.ensure_clean_base(repo, "develop")

    (repo / "untracked-cache.txt").write_text("x")  # untracked files (caches) do not block
    gitops.ensure_clean_base(repo, "develop")

    (repo / "app.py").write_text("x = 99\n")
    with pytest.raises(gitops.GitError, match="clean"):
        gitops.ensure_clean_base(repo, "develop")


def test_python_caches_are_never_committed(repo, tmp_path):
    wt = gitops.create_worktree(repo, tmp_path / "wt", "T-3", attempt=1, base="develop")
    for rel in ("__pycache__/app.cpython-312.pyc", "pkg/__pycache__/m.pyc", ".pytest_cache/v/cache/x",
                ".ruff_cache/0/x", ".venv/bin/python"):
        target = wt.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("cache")

    assert gitops.commit_all(wt.path, "caches only") is False
