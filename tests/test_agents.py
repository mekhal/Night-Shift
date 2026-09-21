import sys
from datetime import UTC, datetime, timedelta

from nightshift.agents import ProcResult, detect_usage_limit, render, run, unavailable

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


def test_run_captures_output_and_stdin(tmp_path):
    code = "import sys; data = sys.stdin.read(); print('out:' + data); print('err', file=sys.stderr); sys.exit(3)"
    result = run([sys.executable, "-c", code], cwd=tmp_path, timeout=30, stdin_text="hello")

    assert result.code == 3
    assert result.out.strip() == "out:hello"
    assert result.err.strip() == "err"
    assert result.timed_out is False


def test_run_kills_process_group_on_timeout(tmp_path):
    marker = tmp_path / "child-finished"
    code = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3); open(r\"{marker}\", \"w\").close()']);"
        "time.sleep(30)"
    )
    result = run([sys.executable, "-c", code], cwd=tmp_path, timeout=1)

    assert result.timed_out is True
    assert result.seconds < 10
    import time

    time.sleep(4)
    assert not marker.exists()  # grandchild was killed with the group


def test_run_reports_missing_program(tmp_path):
    result = run(["definitely-not-a-program-xyz"], cwd=tmp_path, timeout=5)

    assert result.code == 127
    assert "not found" in result.err


def test_render_replaces_placeholders():
    assert render(["codex", "--cd", "{worktree}", "x{worktree}y"], worktree="/w") == ["codex", "--cd", "/w", "x/wy"]


def test_call_agent_raises_on_usage_limit_only_when_the_agent_fails(tmp_path):
    import pytest

    from nightshift.agents import UsageLimitHit, call_agent

    failing = [sys.executable, "-c", "import sys; print('usage limit reached, try again in 1 hours'); sys.exit(1)"]
    with pytest.raises(UsageLimitHit) as hit:
        call_agent(failing, cwd=tmp_path, timeout=30, prompt="x", now=NOW)
    assert hit.value.limit.reset_at == NOW + timedelta(hours=1)

    talking = [sys.executable, "-c", "print('added handling for the API rate limit')"]
    assert call_agent(talking, cwd=tmp_path, timeout=30, prompt="x", now=NOW).code == 0


def test_no_limit_in_normal_output():
    assert detect_usage_limit("Tests failed: 2", NOW) is None


def test_claude_epoch_style_limit():
    limit = detect_usage_limit("Claude AI usage limit reached|1789653600", NOW)

    assert limit is not None
    assert limit.reset_at == datetime.fromtimestamp(1789653600, UTC)


def test_try_again_in_relative_time():
    limit = detect_usage_limit("You've hit your usage limit. Try again in 2 hours 5 minutes.", NOW)

    assert limit.reset_at == NOW + timedelta(hours=2, minutes=5)


def test_resets_at_clock_time_with_zone():
    limit = detect_usage_limit("5-hour limit reached ∙ resets 3pm (Asia/Bangkok)", NOW)

    # 10:00 UTC is 17:00 in Bangkok, so 3pm Bangkok is tomorrow 08:00 UTC
    assert limit.reset_at == datetime(2026, 9, 18, 8, 0, tzinfo=UTC)


def test_clock_time_that_just_passed_is_unknown_not_tomorrow():
    # Real Codex message, repeated right at 5:25 PM: rolling it to tomorrow made the loop wait ~24 hours.
    at_reset = datetime(2026, 9, 17, 10, 25, tzinfo=UTC)  # 17:25 in Bangkok
    text = "ERROR: You've hit your usage limit. Upgrade to Pro ... or try again at 5:25 PM."

    limit = detect_usage_limit(text.replace("5:25 PM", "5:25 PM (Asia/Bangkok)"), at_reset)
    assert limit is not None and limit.reset_at is None

    later = detect_usage_limit(text.replace("5:25 PM", "5:25 PM (Asia/Bangkok)"), at_reset + timedelta(minutes=40))
    assert later.reset_at is None


def test_claude_session_limit_is_recognised():
    # Real Claude Code message (2026-09-18). "session limit" matched none of the patterns, so a Claude that was
    # out of quota looked like a crashed implementer: WEB-02 burned all three attempts in 24 seconds.
    at_hit = datetime(2026, 9, 18, 4, 5, tzinfo=UTC)  # 11:05 in Bangkok

    limit = detect_usage_limit("You've hit your session limit · resets 12:10pm (Asia/Bangkok)", at_hit)

    assert limit is not None
    assert limit.reset_at == datetime(2026, 9, 18, 5, 10, tzinfo=UTC)


def test_limit_without_parseable_time():
    limit = detect_usage_limit("ERROR: rate limit exceeded for this plan", NOW)

    assert limit is not None and limit.reset_at is None


def test_the_gemini_cli_wordings_are_read_as_usage_limits():
    now = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)

    for text in (
        "Quota exceeded for quota metric 'Gemini 2.5 Pro Requests' and limit 'PerDayPerProject'",
        "You have reached your daily limit for gemini-2.5-pro.",
        "ApiError: 429 RESOURCE_EXHAUSTED: Resource has been exhausted (e.g. check quota).",
    ):
        assert detect_usage_limit(text, now) is not None, text


def proc(out="", err="", code=1, timed_out=False):
    return ProcResult(code=code, out=out, err=err, timed_out=timed_out, seconds=1.0)


def test_an_agent_without_a_login_is_reported_as_unable_to_run():
    # Real Gemini CLI output, 2026-09-20, exit 41: it never started, so it must not cost the task an attempt.
    err = ("YOLO mode is enabled. All tool calls will be automatically approved.\n"
           "Please set an Auth method in your /home/agent/.gemini/settings.json or specify one of the following "
           "environment variables before running: GEMINI_API_KEY, GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_GENAI_USE_GCA")

    assert "Auth method" in unavailable(proc(err=err, code=41))
    assert unavailable(proc(err="gemini: command not found", code=127)) is not None
    assert unavailable(proc(out="wrote src/calc.py", code=0)) is None


def test_an_agent_whose_account_can_no_longer_use_it_is_reported_as_unable_to_run():
    # Real Gemini CLI output, 2026-09-21, exit 1: the free tier dropped the CLI. It wrote nothing, so the chain
    # must move on to the next agent instead of failing the cycle (it opened the circuit breaker on DIAG-01).
    err = ("YOLO mode is enabled. All tool calls will be automatically approved.\n"
           "Error authenticating: IneligibleTierError: This client is no longer supported for Gemini Code Assist "
           "for individuals. To continue using Gemini, please migrate to the Antigravity suite of products\n"
           "    at throwIneligibleOrProjectIdError (file:///x/chunk.js:310656:11)")

    assert "IneligibleTierError" in unavailable(proc(err=err, code=1))


def test_a_normal_failure_is_not_mistaken_for_a_missing_login():
    assert unavailable(proc(err="AssertionError: test_add failed", code=1)) is None
    assert unavailable(proc(err="hit your usage limit, try again in 2 hours", code=1)) is None


def test_a_multi_day_limit_keeps_its_named_date():
    # Real Codex output, 2026-09-20. Without the date the loop would re-try it every 5 minutes for two days.
    text = ("ERROR: You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
            "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at "
            "Sep 22nd, 2026 10:35 PM.")

    limit = detect_usage_limit(text, NOW)

    local = limit.reset_at.astimezone(datetime.now().astimezone().tzinfo)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 9, 22, 22, 35)


def test_a_named_date_that_has_already_passed_is_ignored():
    text = "usage limit reached, try again at Sep 1st, 2026 10:35 PM"

    limit = detect_usage_limit(text, NOW)

    assert limit is not None and limit.reset_at is None  # falls back to the quota poll
