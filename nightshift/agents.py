"""Run external agents (codex, gemini, claude) and gate commands; detect subscription usage limits."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ProcResult:
    code: int
    out: str
    err: str
    timed_out: bool
    seconds: float

    @property
    def text(self) -> str:
        return self.out + "\n" + self.err


def render(cmd: list[str], **values: str) -> list[str]:
    out = []
    for part in cmd:
        for key, value in values.items():
            part = part.replace("{" + key + "}", value)
        out.append(part)
    return out


def run(cmd: list[str], cwd: Path, timeout: float, stdin_text: str | None = None) -> ProcResult:
    """Run in a new process group so a timeout kills the agent and everything it started."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError:
        return ProcResult(127, "", f"{cmd[0]}: command not found", False, 0.0)

    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        timed_out = True
    return ProcResult(proc.returncode, out or "", err or "", timed_out, time.monotonic() - start)


# usage limits ---------------------------------------------------------------

_LIMIT = re.compile(
    # `hit your <word> limit` covers Claude's "session limit" and Codex's "usage limit" alike; a missed wording
    # is expensive, because the agent then looks like it crashed and the task burns its attempts at full speed.
    # The Gemini CLI words it differently again: "Quota exceeded for quota metric ...", "daily limit",
    # and the raw API status RESOURCE_EXHAUSTED.
    r"usage limit|rate limit|session limit|hit your (?:\w+ )?limit|limit reached|quota exceeded|out of credits"
    r"|quota limit|daily limit|resource[ _](?:has been )?exhausted",
    re.I,
)
_EPOCH = re.compile(r"limit reached\|(\d{9,11})", re.I)
_RELATIVE = re.compile(r"try again in\s+(?:(\d+)\s*hours?)?(?:\s*,?\s*(?:and\s+)?)?(?:(\d+)\s*min(?:ute)?s?)?", re.I)
_CLOCK = re.compile(
    r"(?:resets?|try again at)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([A-Za-z_]+(?:/[A-Za-z_]+)*)\))?",
    re.I,
)
# A limit that lasts days names the date too: "try again at Sep 22nd, 2026 10:35 PM" (Codex, 2026-09-20).
# Without this the reset reads as "unknown" and the agent is re-tried every quota_poll_s for two days.
_DATE_CLOCK = re.compile(
    r"(?:resets?|try again at)\s+(?:at\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
    r"[\s,]+(\d{1,2}):(\d{2})\s*(am|pm)?",
    re.I,
)


# An implementer that is not installed or not logged in never wrote a line of code, so it must not cost the task
# an attempt: the chain skips it and the next agent writes instead.
_UNAVAILABLE = re.compile(
    r"command not found|not (?:installed|authenticated|logged in)|please (?:sign|log) ?in"
    r"|login required|no (?:credentials|api key)|set (?:the )?\w*api[_ ]key"
    # The Gemini CLI asks for an auth method and names the environment variables it would accept (exit 41).
    r"|auth(?:entication)? method|specify one of the following environment variables"
    r"|\b(?:GEMINI|GOOGLE_\w+|ANTHROPIC|OPENAI)_API_KEY\b"
    # The account may no longer use this client at all (Gemini CLI free tier, 2026-09-21).
    r"|error authenticating|ineligible ?tier|client is no longer supported",
    re.I,
)


def unavailable(result: ProcResult) -> str | None:
    """Why this agent could not run at all, or None when it ran (whatever it then said)."""
    if result.timed_out:
        return None
    if result.code == 127:
        return result.text.strip().splitlines()[-1] if result.text.strip() else "command not found"
    if result.code != 0 and (match := _UNAVAILABLE.search(result.text[-3000:])):
        line = next((ln.strip() for ln in result.text.splitlines() if _UNAVAILABLE.search(ln)), match.group(0))
        return line
    return None


@dataclass(frozen=True)
class UsageLimit:
    reset_at: datetime | None
    message: str


def _clock_reset(match: re.Match, now: datetime) -> datetime | None:
    hour, minute, ampm, zone = match.groups()
    if ampm is None and minute is None:
        return None
    hour, minute = int(hour), int(minute or 0)
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        tz = ZoneInfo(zone) if zone else datetime.now().astimezone().tzinfo
    except ZoneInfoNotFoundError:
        tz = datetime.now().astimezone().tzinfo
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        if local_now - candidate < timedelta(hours=1):
            return None  # the stated time just passed but the limit remains: retry soon instead of waiting a day
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _date_reset(match: re.Match, now: datetime) -> datetime | None:
    """A named date and time, read in the machine's local zone (the agent prints local times)."""
    month, day, year, hour, minute, ampm = match.groups()
    text = f"{month[:3]} {day} {year} {hour}:{minute}"
    fmt = "%b %d %Y %I:%M %p" if ampm else "%b %d %Y %H:%M"
    try:
        parsed = datetime.strptime(f"{text} {ampm.upper()}" if ampm else text, fmt)
    except ValueError:
        return None
    reset = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(UTC)
    return reset if reset > now else None  # a date already past says nothing about when to retry


class UsageLimitHit(RuntimeError):
    def __init__(self, limit: UsageLimit):
        super().__init__(limit.message)
        self.limit = limit


def call_agent(cmd: list[str], cwd: Path, timeout: float, prompt: str, now: datetime) -> ProcResult:
    """Run an agent with the prompt on stdin. A failed run whose output mentions a limit raises UsageLimitHit.

    Output of a successful run is not checked: agents may legitimately write about rate limits.
    """
    result = run(cmd, cwd=cwd, timeout=timeout, stdin_text=prompt)
    if result.code != 0 and not result.timed_out:
        if limit := detect_usage_limit(result.text[-3000:], now):
            raise UsageLimitHit(limit)
    return result


def detect_usage_limit(text: str, now: datetime) -> UsageLimit | None:
    match = _LIMIT.search(text)
    if not match:
        return None
    line = next((ln.strip() for ln in text.splitlines() if _LIMIT.search(ln)), match.group(0))

    if m := _EPOCH.search(text):
        return UsageLimit(datetime.fromtimestamp(int(m.group(1)), UTC), line)
    if (m := _DATE_CLOCK.search(text)) and (when := _date_reset(m, now)):
        return UsageLimit(when, line)
    if (m := _RELATIVE.search(text)) and (m.group(1) or m.group(2)):
        return UsageLimit(now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0)), line)
    if m := _CLOCK.search(text):
        return UsageLimit(_clock_reset(m, now), line)
    return UsageLimit(None, line)
