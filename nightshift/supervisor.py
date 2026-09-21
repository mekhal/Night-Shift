"""The loop: pick an eligible task, implement → gates → review → merge, repeat.

`tick()` does one unit of work and returns (action, seconds to wait). `run_forever()` calls it until stopped.
"""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from nightshift import gitops, human
from nightshift.agents import ProcResult, UsageLimit, UsageLimitHit, call_agent, render, unavailable
from nightshift.config import Config, ConfigError, Implementer, load_config
from nightshift.gates import check_diff, parse_diff, run_command_gates
from nightshift.prompts import fix_prompt, implement_prompt, review_prompt
from nightshift.report import append_report
from nightshift.review import Verdict, combine, parse_verdict
from nightshift.store import Store
from nightshift.tasks import Task, TaskFileError, load_tasks

FAILED_OUTCOMES = {"needs_human", "retry", "error"}


class Escalate(Exception):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


class Aborted(Exception):
    """AUTOPILOT was switched off during a cycle."""


class Supervisor:
    def __init__(self, config_path: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.config_path = config_path
        self.now = now
        self.cfg = load_config(config_path)
        self._reviewed_by = ""  # who gave the last review, for the merge trailer
        self.store = Store(self.cfg.state_dir / "nightshift.db")
        # Only one process runs a Supervisor (the CLI holds the lock), so anything in progress was interrupted.
        self.store.recover_interrupted()
        self.store.set_state("current_task", None)

    # loop ------------------------------------------------------------------

    def start_heartbeat(self, stop: threading.Event, interval: float = 30) -> threading.Thread:
        """Beat from a separate thread so long agent steps do not look like a dead supervisor."""
        db_path = self.cfg.state_dir / "nightshift.db"

        def beat() -> None:
            store = Store(db_path)
            while not stop.is_set():
                store.set_state("heartbeat", datetime.now(UTC).isoformat())
                stop.wait(interval)
            store.conn.close()

        thread = threading.Thread(target=beat, name="heartbeat", daemon=True)
        thread.start()
        return thread

    def run_forever(self, stop: threading.Event, max_ticks: int | None = None) -> None:
        self.start_heartbeat(stop)
        ticks = 0
        while not stop.is_set():
            try:
                action, wait = self.tick()
            except Exception:  # never let the loop die; the dashboard shows the error
                self.store.set_state("last_error", traceback.format_exc()[-2000:])
                action, wait = "error", 60
            self.store.set_state("last_action", action)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
            stop.wait(wait)

    def tick(self) -> tuple[str, int]:
        self.store.set_state("heartbeat", self.now().isoformat())
        try:
            self.cfg = load_config(self.config_path)
        except (ConfigError, OSError, ValueError) as exc:
            self.store.set_state("config_error", str(exc))
            return "config-error", 60
        self.store.set_state("config_error", None)
        self.store.set_state("mode", self.cfg.mode)
        limits = self.cfg.limits

        self._apply_answers()
        if self.cfg.mode == "off":
            return "off", limits.off_poll_s
        if self.store.get_state("breaker_open") == "1":
            return "breaker", limits.off_poll_s

        try:
            plan, tasks = load_tasks(self.cfg.tasks_file)
            self.store.sync_tasks(plan, tasks)
            self.store.set_state("tasks_error", None)
        except (TaskFileError, OSError, ValueError) as exc:
            self.store.set_state("tasks_error", str(exc))

        if self.cfg.mode in ("on-limited", "on"):
            self.store.requeue_would_merge()  # dry-run approvals are redone for real
        tiers = {"green", "yellow"} if self.cfg.mode == "on" else {"green"}
        task = self.store.next_eligible(tiers)
        if task is None:
            return "idle", limits.idle_poll_s
        # Checked against the task, not in general: a joint task also needs the second reviewer, and starting
        # without it means implementing the whole task again for nothing when the review step finds the limit.
        if blocked_until := self._blocked_until(task):
            left = (blocked_until - self.now()).total_seconds()
            return "quota-wait", int(min(left, limits.off_poll_s)) or 1
        return "cycle:" + self._cycle(task), 0

    def _apply_answers(self) -> None:
        for answer in human.collect_answers(self.cfg.state_dir / "needs-human"):
            if self.store.get_task(answer.task_id) is None:
                continue
            if answer.decision == "retry":
                self.store.reset_attempts(answer.task_id)
                self.store.set_status(answer.task_id, "ready")
            else:
                self.store.set_status(answer.task_id, "skipped")

    # cycle -----------------------------------------------------------------

    def _cycle(self, task: Task) -> str:
        cfg = self.cfg
        run_id = self.store.start_run(task.id)
        run_dir = cfg.state_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        started = self.now()
        attempt = self.store.increment_attempts(task.id)
        branch = f"task/{task.id}-a{attempt}"
        wt = None
        self.store.set_state("current_task", task.id)
        self.store.set_status(task.id, "in_progress", "worktree")

        try:
            gitops.ensure_clean_base(cfg.repo, cfg.base_branch)
            wt = gitops.create_worktree(cfg.repo, cfg.worktrees, task.id, attempt, cfg.base_branch)
            outcome, detail = self._work(task, wt, run_dir, run_id, attempt)
        except UsageLimitHit as hit:
            # the per-provider limit is already recorded by _agent; this only reports it
            self._undo_attempt(task.id)
            resume = hit.limit.reset_at.isoformat() if hit.limit.reset_at else "unknown"
            outcome, detail = "quota", f"usage limit, resume after {resume}: {hit.limit.message}"
        except Aborted:
            self._undo_attempt(task.id)
            outcome, detail = "aborted", "AUTOPILOT switched off during the cycle"
        except Escalate as esc:
            outcome, detail = self._escalate(task, esc.kind, esc.reason, branch, run_id), esc.reason
        except Exception as exc:
            (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            detail = f"{type(exc).__name__}: {exc}"
            outcome = self._retry_or_escalate(task, "error", detail, branch, run_id, attempt)
        finally:
            if wt is not None:
                gitops.remove_worktree(cfg.repo, wt.path)
            self.store.set_state("current_task", None)

        if outcome in ("quota", "aborted"):
            self.store.set_status(task.id, "ready")
        self._track_failures(outcome, detail)
        self.store.finish_run(run_id, outcome, detail)
        seconds = (self.now() - started).total_seconds()
        append_report(cfg.state_dir / "reports", started.strftime("%Y-%m-%d"), run_id, task.id, outcome, detail, seconds)
        (run_dir / "summary.json").write_text(
            json.dumps({"task": task.id, "attempt": attempt, "outcome": outcome, "detail": detail}, indent=2),
            encoding="utf-8",
        )
        return outcome

    def _work(self, task: Task, wt: gitops.Worktree, run_dir: Path, run_id: str, attempt: int) -> tuple[str, str]:
        cfg = self.cfg
        self._step(task, "implement")
        wrote_code: list[str] = []  # providers that wrote code, in order, for the commit trailers
        result, provider = self._run_implementer(
            "implement", wt,
            implement_prompt(task, cfg.gates, cfg.red_paths, cfg.network_modules, cfg.network_allowed_paths),
            run_dir / "implement.out")
        if result.timed_out or result.code != 0:
            why = "timed out" if result.timed_out else f"exit code {result.code}"
            outcome = self._retry_or_escalate(task, "implement", f"implementer {why}", wt.branch, run_id, attempt)
            return outcome, f"implementer {why}"
        wrote_code.append(provider)
        gitops.commit_all(wt.path, f"{task.id}: {task.title}\n\nImplemented-by: {provider}")

        gate_retry_used = False
        fix_rounds = 0
        review_round = 0
        while True:
            self._step(task, "gates")
            failures = check_diff(parse_diff(gitops.diff_against(wt.path, cfg.base_branch)), task,
                                  cfg.red_paths, cfg.network_modules, cfg.limits.max_diff_lines,
                                  cfg.network_allowed_paths)
            failures += run_command_gates(cfg.gates, wt.path, cfg.timeouts.gates)
            gate_text = "\n\n".join(f"[{f.gate}]\n{f.detail}" for f in failures)
            (run_dir / f"gates-{review_round + 1}.txt").write_text(gate_text or "all gates passed", encoding="utf-8")
            if failures:
                if gate_retry_used:
                    raise Escalate("gate", "gates failed after one fix:\n\n" + gate_text[-3000:])
                gate_retry_used = True
                wrote_code.append(self._fix(task, wt, [f"gate {f.gate}: {f.detail[-1500:]}" for f in failures],
                                            run_dir / f"fix-gates-{review_round + 1}.out"))
                continue

            review_round += 1
            self._step(task, "review")
            verdict = self._review(task, wt, run_dir, review_round)
            if verdict.verdict == "approve":
                break
            if verdict.verdict == "escalate":
                raise Escalate("review", verdict.reason)
            if fix_rounds >= cfg.limits.max_fix_rounds:
                raise Escalate("review", f"still not approved after {fix_rounds} fix rounds: {verdict.reason}")
            fix_rounds += 1
            problems = [f"[{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', '?')} "
                        f"{f.get('issue', '')} — fix: {f.get('fix', '')}" for f in verdict.findings]
            wrote_code.append(self._fix(task, wt, problems or [verdict.reason],
                                        run_dir / f"fix-review-{review_round}.out"))
            gate_retry_used = False  # a review fix may break the gates; it gets one gate retry of its own

        self._check_mode()
        if cfg.mode == "dry-run":
            self.store.set_status(task.id, "would_merge")
            return "would_merge", f"approved; dry-run kept branch {wt.branch}"
        return self._merge(task, wt, run_id, verdict, "+".join(dict.fromkeys(wrote_code)))

    def _fix(self, task: Task, wt: gitops.Worktree, problems: list[str], out: Path) -> str:
        """Returns the provider that did the fixing; it need not be the one that wrote the code."""
        self._step(task, "fix")
        cfg = self.cfg
        prompt = fix_prompt(task, problems, cfg.gates, cfg.red_paths, cfg.network_modules,
                            cfg.network_allowed_paths)
        result, provider = self._run_implementer("fix", wt, prompt, out)
        if result.timed_out or result.code != 0:
            raise Escalate("implement", f"implementer failed while fixing (exit {result.code}, "
                                        f"timed out: {result.timed_out})")
        gitops.commit_all(wt.path, f"{task.id}: fix\n\nImplemented-by: {provider}")
        return provider

    def _review(self, task: Task, wt: gitops.Worktree, run_dir: Path, round_no: int) -> Verdict:
        cfg = self.cfg
        prompt = review_prompt(task, gitops.diff_against(wt.path, cfg.base_branch), "all gates passed")
        reviewers = {"claude": ("claude_review", cfg.agents.claude_review)}
        if task.review == "joint":
            reviewers["codex"] = ("codex_review", cfg.agents.codex_review)
        verdicts = {}
        for name, (agent, cmd) in reviewers.items():
            stand_in = agent == "codex_review" and bool(cfg.agents.codex_review_fallback)
            if stand_in and self._limited_until(cfg.agents.provider_of(agent)):
                name, agent, cmd = self._codex_stand_in()
            try:
                result = self._agent("review", cmd, wt.path, cfg.timeouts.review, prompt,
                                     run_dir / f"review-{round_no}-{name}.out", cfg.agents.provider_of(agent))
            except UsageLimitHit:
                if not stand_in or agent != "codex_review":
                    raise
                name, agent, cmd = self._codex_stand_in()  # Codex ran out mid-cycle: keep the finished work
                result = self._agent("review", cmd, wt.path, cfg.timeouts.review, prompt,
                                     run_dir / f"review-{round_no}-{name}.out", cfg.agents.provider_of(agent))
            if result.timed_out or result.code != 0:
                verdicts[name] = Verdict("escalate", "yellow", [], f"{name} reviewer failed (exit {result.code}, "
                                                                    f"timed out: {result.timed_out})")
            else:
                verdicts[name] = parse_verdict(result.out)
        verdict = combine(verdicts)
        (run_dir / f"review-{round_no}.json").write_text(
            json.dumps({"combined": asdict(verdict), **{k: asdict(v) for k, v in verdicts.items()}}, indent=2),
            encoding="utf-8",
        )
        self._reviewed_by = "+".join(verdicts)
        return verdict

    def _codex_stand_in(self) -> tuple[str, str, list[str]]:
        """The reviewer that takes the Codex seat while Codex is limited; named so git history shows it."""
        provider = self.cfg.agents.provider_of("codex_review_fallback")
        return f"codex-fallback:{provider}", "codex_review_fallback", self.cfg.agents.codex_review_fallback

    def _merge(self, task: Task, wt: gitops.Worktree, run_id: str, verdict: Verdict,
               implemented_by: str) -> tuple[str, str]:
        cfg = self.cfg
        self._step(task, "merge")
        row = self.store.get_task(task.id)
        reviewers = self._reviewed_by or ("claude+codex" if task.review == "joint" else "claude")
        message = (
            f"Merge {wt.branch}: {task.id} {task.title}\n\n"
            f"Task: {task.id}\nPlan: {row['plan']}\nRisk: {verdict.risk}\nDecided-by: ai:reviewer\n"
            f"Reviewed-by: {reviewers}\nImplemented-by: {implemented_by}\n"
            f"Review-run: {cfg.state_dir / 'runs' / run_id}\n"
        )
        gitops.ensure_clean_base(cfg.repo, cfg.base_branch)
        sha = gitops.merge(cfg.repo, wt.branch, message)

        self._step(task, "post-merge gates")
        failures = run_command_gates(cfg.gates, cfg.repo, cfg.timeouts.gates)
        if failures:
            gitops.revert_merge(cfg.repo, sha)
            text = "\n\n".join(f"[{f.gate}]\n{f.detail}" for f in failures)
            self.store.set_state("breaker_open", "1")
            self.store.set_state("breaker_reason", f"post-merge gates failed for {task.id}; merge {sha[:10]} reverted")
            raise Escalate("merge", f"post-merge gates failed on {cfg.base_branch}; merge {sha[:10]} was reverted.\n\n"
                                    + text[-3000:])
        self.store.set_status(task.id, "merged")
        return "merged", f"merged {sha[:10]}"

    # usage limits ----------------------------------------------------------

    def _limited_until(self, provider: str) -> datetime | None:
        """When `provider` is free again, or None. A limit that has run out is cleared on the way past."""
        raw = self.store.get_state(f"limit:{provider}:resume_after")
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        if until > self.now():
            return until
        self.store.set_state(f"limit:{provider}:resume_after", None)
        self.store.set_state(f"limit:{provider}:message", None)
        return None

    def _record_limit(self, provider: str, limit: UsageLimit) -> datetime:
        """An agent that names no reset time is re-checked after quota_poll_s rather than guessed at."""
        reset = limit.reset_at or self.now() + timedelta(seconds=self.cfg.limits.quota_poll_s)
        self.store.set_state(f"limit:{provider}:resume_after", reset.isoformat())
        self.store.set_state(f"limit:{provider}:message", limit.message)
        return reset

    def _implementers(self) -> list[Implementer]:
        """Who writes the code, in preference order; a fallback only covers the limits of the links before it."""
        return self.cfg.agents.implement_chain()

    def _blocked_until(self, task: Task) -> datetime | None:
        """The earliest moment this task could run, or None when every agent it needs has quota left."""
        blocking = []
        implementers = {i.provider: self._limited_until(i.provider) for i in self._implementers()}
        if all(implementers.values()):
            blocking += list(implementers.values())
        roles = ["claude_review"] + (["codex_review"] if task.review == "joint" else [])
        for role in roles:
            if until := self._limited_until(self.cfg.agents.provider_of(role)):
                stand_in = self.cfg.agents.codex_review_fallback and role == "codex_review"
                if stand_in and not self._limited_until(self.cfg.agents.provider_of("codex_review_fallback")):
                    continue  # the fallback reviewer takes the Codex seat
                blocking.append(until)
        return min(blocking) if blocking else None

    def _run_implementer(self, step: str, wt: gitops.Worktree, prompt: str, out: Path) -> tuple[ProcResult, str]:
        """Run the first implementer with quota left. Raises UsageLimitHit only when every one is out."""
        hit: list[tuple[str, datetime | None, str]] = []  # provider, reset, message
        limited = False  # at least one agent has quota to come back to
        could_not_run: tuple[ProcResult, str] | None = None
        for implementer in self._implementers():
            provider, cmd = implementer.provider, implementer.cmd
            until = self._limited_until(provider)
            if until is None:
                try:
                    result = self._agent(step, cmd, wt.path, self.cfg.timeouts.implement, prompt,
                                         out.with_name(f"{out.stem}-{provider}{out.suffix}"), provider)
                    if why := unavailable(result):
                        # not installed or not logged in: it wrote nothing, so the next agent takes over
                        hit.append((provider, None, f"cannot run: {why}"))
                        could_not_run = (result, provider)
                        continue
                    return result, provider
                except UsageLimitHit:
                    until = self._limited_until(provider)  # _agent recorded it
            limited = True
            hit.append((provider, until, self.store.get_state(f"limit:{provider}:message") or "usage limit"))
        if not limited and could_not_run:
            # No limit to wait for: the chain itself is broken, so fail the cycle and let a human fix the setup.
            return could_not_run
        resets = [until for _, until, _ in hit if until]
        messages = "; ".join(f"{provider}: {message}" for provider, _, message in hit)
        raise UsageLimitHit(UsageLimit(min(resets) if resets else None, messages))

    # helpers ---------------------------------------------------------------

    def _agent(self, step: str, cmd: list[str], cwd: Path, timeout: int, prompt: str, out: Path, provider: str):
        self._check_mode()
        try:
            result = call_agent(render(cmd, worktree=str(cwd)), cwd=cwd, timeout=timeout, prompt=prompt, now=self.now())
        except UsageLimitHit as hit:
            self._record_limit(provider, hit.limit)
            out.write_text(f"[usage limit] {hit.limit.message}\n", encoding="utf-8")
            raise
        out.write_text(f"$ exit={result.code} timed_out={result.timed_out} seconds={result.seconds:.0f}\n"
                       f"--- stdout\n{result.out}\n--- stderr\n{result.err}\n", encoding="utf-8")
        return result

    def _step(self, task: Task, step: str) -> None:
        self._check_mode()
        self.store.set_step(task.id, step)
        self.store.set_state("step_started_at", self.now().isoformat())

    def _check_mode(self) -> None:
        try:
            mode = load_config(self.config_path).mode
        except (ConfigError, OSError, ValueError):
            return
        if mode == "off":
            raise Aborted()
        self.cfg = load_config(self.config_path)

    def _undo_attempt(self, task_id: str) -> None:
        self.store.conn.execute("UPDATE tasks SET attempts = MAX(attempts - 1, 0) WHERE task_id = ?", (task_id,))

    def _escalate(self, task: Task, kind: str, reason: str, branch: str, run_id: str) -> str:
        self.store.set_status(task.id, "needs_human")
        human.write_escalation(self.cfg.state_dir / "needs-human", task.id, kind, reason, branch, run_id)
        return "needs_human"

    def _retry_or_escalate(self, task: Task, kind: str, reason: str, branch: str, run_id: str, attempt: int) -> str:
        if attempt >= self.cfg.limits.max_attempts:
            return self._escalate(task, kind, f"{reason} (attempt {attempt} of {self.cfg.limits.max_attempts})",
                                  branch, run_id)
        self.store.set_status(task.id, "ready")
        return "retry"

    def _track_failures(self, outcome: str, detail: str) -> None:
        if outcome in ("merged", "would_merge"):
            self.store.set_state("consecutive_failures", "0")
            return
        if outcome not in FAILED_OUTCOMES:
            return
        count = int(self.store.get_state("consecutive_failures") or 0) + 1
        self.store.set_state("consecutive_failures", str(count))
        if count >= self.cfg.limits.breaker_threshold and self.store.get_state("breaker_open") != "1":
            self.store.set_state("breaker_open", "1")
            self.store.set_state("breaker_reason", f"{count} failed cycles in a row; last: {detail[:300]}")
