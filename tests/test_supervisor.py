import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nightshift.store import Store
from nightshift.supervisor import Supervisor

FAKE = str(Path(__file__).with_name("fake_agent.py"))
PY = sys.executable

GOOD_CODE = {"src/calc.py": "def add(a, b):\n    return a + b\n",
             "tests/test_calc.py": "def test_add():\n    assert True\n"}
APPROVE = json.dumps({"verdict": "approve", "risk": "green", "findings": [], "reason": "looks right"})
CHANGES = json.dumps({"verdict": "changes", "risk": "green",
                      "findings": [{"severity": "medium", "file": "src/calc.py", "line": 1,
                                    "issue": "missing docstring", "fix": "add one"}], "reason": "small fix"})
ESCALATE = json.dumps({"verdict": "escalate", "risk": "yellow", "findings": [], "reason": "criteria unclear"})

TASKS = '''
plan = "docs/superpowers/plans/p.md"

[[task]]
id = "T-1"
title = "Add calc"
tier = "green"
review = "{review}"
paths = ["src/**", "tests/**"]
body = "Add add()."
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class Env:
    def __init__(self, tmp: Path, review="technical", mode="on", fallback=False, chain=False, review_fallback=False):
        self.fallback = fallback
        self.review_fallback = review_fallback
        self.chain = chain
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "develop")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@localhost")
        (self.repo / "plan.tasks.toml").write_text(TASKS.format(review=review))
        (self.repo / "README.md").write_text("repo\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.scenario = tmp / "scenario" / "scenario.json"
        self.scenario.parent.mkdir()
        self.state = tmp / "state"
        self.config = tmp / "config.toml"
        self.now = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
        self.write_config(mode)

    def write_config(self, mode):
        gate = ("import os, sys; bad = os.path.exists('src/fail_gate.txt') or "
                "(os.path.exists('src/fail_after_merge.txt') and os.path.isdir('.git')); sys.exit(1 if bad else 0)")
        fallback = ""
        if self.fallback:
            fallback = "implement_fallback = " + json.dumps([PY, FAKE, str(self.scenario), "claude_impl"]) + "\n"
        if self.review_fallback:
            fallback += "codex_review_fallback = " + json.dumps([PY, FAKE, str(self.scenario), "sonnet_review"]) + "\n"
        chain = ""
        if self.chain:
            for provider, role in (("gemini", "gemini_impl"), ("claude", "claude_impl")):
                chain += "\n".join([
                    "",
                    "[[agents.implement_fallbacks]]",
                    f'provider = "{provider}"',
                    "cmd = " + json.dumps([PY, FAKE, str(self.scenario), role]),
                    "",
                ])
        self.config.write_text(f'''
mode = "{mode}"
repo = "{self.repo.as_posix()}"
tasks_file = "plan.tasks.toml"
state_dir = "{self.state.as_posix()}"
worktrees = "{(self.tmp / 'wt').as_posix()}"

[[gates]]
name = "unit"
cmd = {json.dumps([PY, "-c", gate])}

[agents]
implement = {json.dumps([PY, FAKE, str(self.scenario), "implement"])}
{fallback}claude_review = {json.dumps([PY, FAKE, str(self.scenario), "claude"])}
codex_review = {json.dumps([PY, FAKE, str(self.scenario), "codex_review"])}
{chain}
[limits]
breaker_threshold = 3
''')

    def play(self, **roles):
        self.scenario.write_text(json.dumps(roles))

    def supervisor(self):
        return Supervisor(self.config, now=lambda: self.now)

    def store(self):
        return Store(self.state / "nightshift.db")

    def task(self):
        return self.store().get_task("T-1")

    def prompt(self, role, n):
        return (self.scenario.parent / f"{role}-{n}.prompt").read_text()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_happy_path_merges_with_trailers(env):
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])

    action, wait = env.supervisor().tick()

    assert action == "cycle:merged" and wait == 0
    assert env.task()["status"] == "merged"
    assert (env.repo / "src" / "calc.py").exists()
    message = git(env.repo, "log", "-1", "--format=%B")
    assert "Task: T-1" in message and "Decided-by: ai:reviewer" in message and "Implemented-by: codex" in message
    assert len(git(env.repo, "log", "-1", "--format=%P").split()) == 2
    assert not (env.tmp / "wt" / "T-1").exists()
    assert "T-1" in env.prompt("implement", 0) and "diff --git" in env.prompt("claude", 0)
    run = env.store().recent_runs(1)[0]
    assert run["outcome"] == "merged"
    assert (env.state / "runs" / run["run_id"] / "review-1.json").exists()
    assert "T-1" in next((env.state / "reports").glob("*.md")).read_text()

    assert env.supervisor().tick() == ("idle", 300)


def test_dry_run_does_not_merge(tmp_path):
    env = Env(tmp_path, mode="dry-run")
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:would_merge"
    assert env.task()["status"] == "would_merge"
    assert not (env.repo / "src").exists()
    assert "task/T-1-a1" in git(env.repo, "branch", "--list", "task/T-1-a1")


def test_gate_failure_is_sent_back_once_then_passes(env):
    env.play(
        implement=[{"files": {**GOOD_CODE, "src/fail_gate.txt": "x"}},
                   {"files": {"src/fail_gate.txt": ""}, "stdout": "removed"}],
        claude=[{"stdout": APPROVE}],
    )
    # the fix deletes the marker by emptying it; make the gate treat an empty file as fixed
    env.write_config("on")
    gate_fix = env.config.read_text().replace("os.path.exists('src/fail_gate.txt')",
                                              "os.path.getsize('src/fail_gate.txt') > 0 if os.path.exists('src/fail_gate.txt') else False")
    env.config.write_text(gate_fix)

    assert env.supervisor().tick()[0] == "cycle:merged"
    assert "unit" in env.prompt("implement", 1)


def test_gate_failing_twice_escalates(env):
    env.play(implement=[{"files": {**GOOD_CODE, "src/fail_gate.txt": "x"}}], claude=[{"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:needs_human"
    assert env.task()["status"] == "needs_human"
    text = (env.state / "needs-human" / "T-1.md").read_text()
    assert "type: gate" in text and "unit" in text
    assert not (env.scenario.parent / "claude.count").exists()  # never reviewed


def test_review_changes_then_approve(env):
    env.play(implement=[{"files": GOOD_CODE}, {"files": {"src/calc.py": '"""Calc."""\n' + GOOD_CODE["src/calc.py"]}}],
             claude=[{"stdout": CHANGES}, {"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:merged"
    assert "missing docstring" in env.prompt("implement", 1)
    assert (env.repo / "src" / "calc.py").read_text().startswith('"""Calc."""')


def test_gate_break_from_a_review_fix_gets_its_own_retry(env):
    # APP-08: gates failed once and were fixed, review asked for changes, the review fix broke the gates.
    empty_marker = "os.path.getsize('src/fail_gate.txt') > 0 if os.path.exists('src/fail_gate.txt') else False"
    env.config.write_text(env.config.read_text().replace("os.path.exists('src/fail_gate.txt')", empty_marker))
    env.play(
        implement=[
            {"files": {**GOOD_CODE, "src/fail_gate.txt": "x"}},  # gates fail
            {"files": {"src/fail_gate.txt": ""}},                # gate fix
            {"files": {"src/fail_gate.txt": "broken again"}},    # review fix breaks gates
            {"files": {"src/fail_gate.txt": ""}},                # second gate fix
        ],
        claude=[{"stdout": CHANGES}, {"stdout": APPROVE}],
    )

    assert env.supervisor().tick()[0] == "cycle:merged"


def test_review_escalation(env):
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": ESCALATE}])

    assert env.supervisor().tick()[0] == "cycle:needs_human"
    assert "criteria unclear" in (env.state / "needs-human" / "T-1.md").read_text()


def test_invalid_reviewer_output_escalates(env):
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": "LGTM!"}])

    assert env.supervisor().tick()[0] == "cycle:needs_human"


def test_joint_review_requires_both_reviewers(tmp_path):
    env = Env(tmp_path, review="joint")
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}], codex_review=[{"stdout": CHANGES}])

    assert env.supervisor().tick()[0] == "cycle:needs_human"
    assert "disagree" in (env.state / "needs-human" / "T-1.md").read_text()


LIMIT_2H = {"stdout": "You've hit your usage limit. Try again in 2 hours.", "exit": 1}
LIMIT_NO_TIME = {"stdout": "You've hit your usage limit.", "exit": 1}


def test_implement_falls_back_to_the_second_agent_when_the_first_is_limited(tmp_path):
    env = Env(tmp_path, fallback=True)
    env.play(implement=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:merged"
    assert "Implemented-by: claude" in git(env.repo, "log", "-1", "--format=%B")
    assert "T-1" in env.prompt("claude_impl", 0)


def test_a_limited_provider_does_not_block_the_one_that_is_still_free(tmp_path):
    env = Env(tmp_path, fallback=True)
    env.play(implement=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    sup = env.supervisor()
    sup.tick()

    assert env.store().get_state("limit:codex:resume_after") == (env.now + timedelta(hours=2)).isoformat()
    assert env.store().get_state("limit:claude:resume_after") is None
    assert sup.tick() == ("idle", 300)  # codex is still limited, but nothing is waiting on it


def test_codex_takes_the_work_back_once_its_limit_resets(tmp_path):
    # The fallback exists only to cover a limit; priority must return to the first implementer afterwards.
    env = Env(tmp_path, fallback=True)
    env.play(implement=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    sup = env.supervisor()

    assert sup.tick()[0] == "cycle:merged"
    assert (env.scenario.parent / "claude_impl.count").exists()  # the fallback wrote this one

    env.now += timedelta(hours=2, seconds=1)
    env.store().set_status("T-1", "ready")
    second = {"src/calc.py": GOOD_CODE["src/calc.py"] + "\n\ndef sub(a, b):\n    return a - b\n",
              "tests/test_calc.py": GOOD_CODE["tests/test_calc.py"] + "\n\ndef test_sub():\n    assert True\n"}
    env.play(implement=[{"files": second}],
             claude_impl=[{"stdout": "the fallback must not run while codex has quota", "exit": 1}],
             claude=[{"stdout": APPROVE}])
    for name in ("implement.count", "claude_impl.count", "claude.count"):
        (env.scenario.parent / name).unlink()

    assert sup.tick()[0] == "cycle:merged"
    assert not (env.scenario.parent / "claude_impl.count").exists()
    assert env.store().get_state("limit:codex:resume_after") is None  # the spent limit was cleared


def test_quota_only_when_every_implementer_is_limited(tmp_path):
    env = Env(tmp_path, fallback=True)
    env.play(implement=[LIMIT_2H], claude_impl=[LIMIT_2H])
    sup = env.supervisor()

    assert sup.tick() == ("cycle:quota", 0)
    task = env.task()
    assert task["status"] == "ready" and task["attempts"] == 0
    for provider in ("codex", "claude"):
        assert env.store().get_state(f"limit:{provider}:resume_after") == (env.now + timedelta(hours=2)).isoformat()
    assert sup.tick()[0] == "quota-wait"


def test_a_limit_without_a_reset_time_is_rechecked_after_the_quota_poll(env):
    env.play(implement=[LIMIT_NO_TIME])
    sup = env.supervisor()

    assert sup.tick() == ("cycle:quota", 0)
    assert env.store().get_state("limit:codex:resume_after") == (env.now + timedelta(seconds=300)).isoformat()
    assert sup.tick()[0] == "quota-wait"

    env.now += timedelta(seconds=301)
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    (env.scenario.parent / "implement.count").unlink()
    assert sup.tick()[0] == "cycle:merged"


def test_usage_limit_waits_without_counting_an_attempt(env):
    env.play(implement=[LIMIT_2H])
    sup = env.supervisor()

    assert sup.tick() == ("cycle:quota", 0)
    task = env.task()
    assert task["status"] == "ready" and task["attempts"] == 0
    assert env.store().get_state("limit:codex:resume_after") == (env.now + timedelta(hours=2)).isoformat()

    action, wait = sup.tick()
    assert action == "quota-wait" and 0 < wait <= 60

    env.now += timedelta(hours=2, seconds=1)
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    (env.scenario.parent / "implement.count").unlink()
    assert sup.tick()[0] == "cycle:merged"


def test_mode_off_does_nothing(tmp_path):
    env = Env(tmp_path, mode="off")

    assert env.supervisor().tick() == ("off", 60)
    assert env.task() is None or env.task()["status"] == "ready"


def test_post_merge_failure_reverts_and_opens_breaker(env):
    env.play(implement=[{"files": {**GOOD_CODE, "src/fail_after_merge.txt": "x"}}], claude=[{"stdout": APPROVE}])
    sup = env.supervisor()

    assert sup.tick()[0] == "cycle:needs_human"
    assert not (env.repo / "src" / "calc.py").exists()  # reverted
    assert "Revert" in git(env.repo, "log", "-1", "--format=%s")
    assert "type: merge" in (env.state / "needs-human" / "T-1.md").read_text()
    assert sup.tick() == ("breaker", 60)


def test_answer_retry_requeues_task(env):
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": ESCALATE}])
    sup = env.supervisor()
    sup.tick()
    answer = env.state / "needs-human" / "T-1.md"
    answer.write_text(answer.read_text().replace("decision:", "decision: A"))
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    for name in ("implement.count", "claude.count"):
        (env.scenario.parent / name).unlink()

    assert sup.tick()[0] == "cycle:merged"
    assert env.task()["attempts"] == 1


def test_interrupted_cycle_is_recovered_on_start(env):
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    sup = env.supervisor()
    sup.tick()  # sync tasks, merge T-1
    store = env.store()
    store.set_status("T-1", "in_progress", "implement")
    run_id = store.start_run("T-1")
    store.set_state("current_task", "T-1")

    env.supervisor()  # a new process starts

    assert env.task()["status"] == "ready" and env.task()["step"] is None
    assert env.store().recent_runs(1)[0]["outcome"] == "interrupted"
    assert env.store().get_state("current_task") is None
    assert run_id


def test_would_merge_tasks_are_requeued_when_merging_is_enabled(tmp_path):
    env = Env(tmp_path, mode="dry-run")
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    sup = env.supervisor()
    assert sup.tick()[0] == "cycle:would_merge"

    env.write_config("on-limited")
    assert sup.tick()[0] == "cycle:merged"
    assert env.task()["attempts"] == 1


def test_heartbeat_is_written_while_a_long_step_runs(env):
    import threading

    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])
    sup = Supervisor(env.config)  # real clock
    stop = threading.Event()
    beat = sup.start_heartbeat(stop, interval=0.1)
    try:
        first = env.store().get_state("heartbeat")
        import time

        time.sleep(0.5)
        assert env.store().get_state("heartbeat") != first
    finally:
        stop.set()
        beat.join(timeout=2)


def test_implementer_crash_retries_until_attempts_exhausted(env):
    env.play(implement=[{"stdout": "segfault", "exit": 139}])
    sup = env.supervisor()

    assert sup.tick()[0] == "cycle:retry"
    assert sup.tick()[0] == "cycle:retry"
    assert sup.tick()[0] == "cycle:needs_human"  # attempt 3 of 3; also the 3rd failed cycle in a row
    assert env.task()["status"] == "needs_human"
    assert "type: implement" in (env.state / "needs-human" / "T-1.md").read_text()
    assert sup.tick() == ("breaker", 60)



def test_the_chain_walks_to_the_next_implementer_for_each_limit(tmp_path):
    # Coding chain agreed with the maintainer 2026-09-20: codex, then gemini, then claude sonnet.
    env = Env(tmp_path, chain=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:merged"
    assert "Implemented-by: gemini" in git(env.repo, "log", "-1", "--format=%B")
    assert not (env.scenario.parent / "claude_impl.count").exists()  # claude is the last resort, not the second


def test_claude_writes_only_when_codex_and_gemini_are_both_limited(tmp_path):
    env = Env(tmp_path, chain=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:merged"
    assert "Implemented-by: claude" in git(env.repo, "log", "-1", "--format=%B")
    assert env.store().get_state("limit:gemini:resume_after") == (env.now + timedelta(hours=2)).isoformat()


def test_quota_only_when_the_whole_chain_is_limited(tmp_path):
    env = Env(tmp_path, chain=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[LIMIT_2H])
    sup = env.supervisor()

    assert sup.tick() == ("cycle:quota", 0)
    assert env.task()["status"] == "ready" and env.task()["attempts"] == 0
    assert sup.tick()[0] == "quota-wait"


NOT_LOGGED_IN = {"stdout": "Please sign in to continue. Run `gemini` and choose an authentication method.", "exit": 1}


def test_an_agent_that_cannot_run_is_skipped_without_costing_an_attempt(tmp_path):
    # A fallback that is installed but not logged in wrote nothing; the next one in the chain takes over.
    env = Env(tmp_path, chain=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[NOT_LOGGED_IN], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:merged"
    assert "Implemented-by: claude" in git(env.repo, "log", "-1", "--format=%B")
    assert env.store().get_state("limit:gemini:resume_after") is None  # nothing to wait for, so no quota state


def test_a_chain_that_can_never_run_fails_the_cycle_instead_of_spinning(tmp_path):
    env = Env(tmp_path, chain=True)
    env.play(implement=[NOT_LOGGED_IN], gemini_impl=[NOT_LOGGED_IN], claude_impl=[NOT_LOGGED_IN])
    sup = env.supervisor()

    assert sup.tick()[0] == "cycle:retry"  # a real failure: it counts an attempt and ends in needs-human
    assert env.task()["attempts"] == 1
    assert sup.tick()[0] != "quota-wait"


def test_a_joint_task_waits_when_the_second_reviewer_has_no_quota(tmp_path):
    # DIAG-01 (2026-09-20): Codex had no quota for its half of a joint review, but the Claude implementer did,
    # so every few minutes a cycle wrote the code again and threw the finished work away at the review step.
    env = Env(tmp_path, review="joint", chain=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}], codex_review=[LIMIT_2H])
    sup = env.supervisor()

    assert sup.tick() == ("cycle:quota", 0)
    assert sup.tick()[0] == "quota-wait"
    assert (env.scenario.parent / "claude_impl.count").read_text() == "1"


def test_a_joint_task_runs_again_once_the_second_reviewer_is_back(tmp_path):
    env = Env(tmp_path, review="joint", chain=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}], codex_review=[LIMIT_2H])
    sup = env.supervisor()
    sup.tick()

    env.now += timedelta(hours=2, seconds=1)
    env.play(implement=[{"files": GOOD_CODE}], gemini_impl=[LIMIT_2H], claude_impl=[LIMIT_2H],
             claude=[{"stdout": APPROVE}], codex_review=[{"stdout": APPROVE}])
    for name in ("implement.count", "claude_impl.count", "claude.count", "codex_review.count"):
        (env.scenario.parent / name).unlink()

    assert sup.tick()[0] == "cycle:merged"


def test_a_technical_task_is_not_held_by_the_second_reviewer(tmp_path):
    # Only a joint task needs Codex; a technical one is reviewed by Claude alone and must keep running.
    env = Env(tmp_path, review="technical", chain=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}], codex_review=[LIMIT_2H])
    sup = env.supervisor()

    assert sup.tick()[0] == "cycle:merged"


def test_a_joint_task_uses_the_review_fallback_while_codex_is_out(tmp_path):
    # Maintainer decision 2026-09-21: while Codex has no quota, Claude Sonnet takes the second review seat, so a
    # joint task keeps flowing instead of waiting days for Codex.
    env = Env(tmp_path, review="joint", chain=True, review_fallback=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}], codex_review=[LIMIT_2H], sonnet_review=[{"stdout": APPROVE}])
    sup = env.supervisor()

    assert sup.tick()[0] == "cycle:merged"
    assert (env.scenario.parent / "claude_impl.count").read_text() == "1"  # the code was written once
    assert "diff --git" in env.prompt("sonnet_review", 0)
    assert "Reviewed-by: claude+codex-fallback:claude" in git(env.repo, "log", "-1", "--format=%B")


def test_a_known_codex_limit_goes_straight_to_the_review_fallback(tmp_path):
    env = Env(tmp_path, review="joint", chain=True, review_fallback=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}], codex_review=[LIMIT_2H], sonnet_review=[{"stdout": APPROVE}])
    sup = env.supervisor()

    assert sup.tick()[0] == "cycle:merged"
    assert not (env.scenario.parent / "codex_review.count").exists()  # Codex was not asked while limited


def test_codex_running_out_during_the_review_hands_over_to_the_fallback(tmp_path):
    env = Env(tmp_path, review="joint", review_fallback=True)
    env.play(implement=[{"files": GOOD_CODE}], claude=[{"stdout": APPROVE}], codex_review=[LIMIT_2H],
             sonnet_review=[{"stdout": APPROVE}])

    assert env.supervisor().tick()[0] == "cycle:merged"
    assert (env.scenario.parent / "codex_review.count").read_text() == "1"
    assert (env.scenario.parent / "implement.count").read_text() == "1"  # the finished work was kept


def test_the_review_fallback_must_agree_too(tmp_path):
    env = Env(tmp_path, review="joint", chain=True, review_fallback=True)
    env.play(implement=[LIMIT_2H], gemini_impl=[LIMIT_2H], claude_impl=[{"files": GOOD_CODE}],
             claude=[{"stdout": APPROVE}], codex_review=[LIMIT_2H], sonnet_review=[{"stdout": ESCALATE}])

    assert env.supervisor().tick()[0] != "cycle:merged"
    assert env.task()["status"] != "merged"
