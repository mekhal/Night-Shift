"""Load `config.toml`. Re-read before every task so mode changes apply without restart."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("off", "dry-run", "on-limited", "on")

DEFAULT_RED_PATHS = [
    ".github/**",
    ".claude/**",
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
    "docs/decisions/**",
    "docs/superpowers/**",
    "docs/ai-dlc/**",
    "AI-Template/**",
    "pyproject.toml",
    "requirements*.txt",
    "*.lock",
]

DEFAULT_NETWORK_MODULES = ["socket", "http.client", "urllib.request", "requests", "httpx", "aiohttp"]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Gate:
    name: str
    cmd: list[str]


@dataclass(frozen=True)
class Timeouts:
    implement: int = 2700
    gates: int = 900
    review: int = 900


@dataclass(frozen=True)
class Limits:
    max_attempts: int = 3
    max_fix_rounds: int = 2
    breaker_threshold: int = 3
    max_diff_lines: int = 400
    quota_poll_s: int = 300  # re-check a usage limit this often when the agent named no reset time
    idle_poll_s: int = 300
    off_poll_s: int = 60


DEFAULT_PROVIDERS = {
    "implement": "codex",
    "implement_fallback": "claude",
    "codex_review": "codex",
    "codex_review_fallback": "claude",
    "claude_review": "claude",
}


@dataclass(frozen=True)
class Implementer:
    """One link of the implement chain: the command, and the subscription it spends."""

    provider: str
    cmd: list[str]


@dataclass(frozen=True)
class Agents:
    implement: list[str] = field(
        default_factory=lambda: ["codex", "exec", "--sandbox", "workspace-write", "--cd", "{worktree}", "-"]
    )
    implement_fallback: list[str] = field(default_factory=list)  # one fallback, used while `implement` is limited
    # Several fallbacks instead, tried in order: `[[agents.implement_fallbacks]]` with `provider` and `cmd`.
    implement_fallbacks: list[Implementer] = field(default_factory=list)
    codex_review: list[str] = field(
        default_factory=lambda: ["codex", "exec", "--sandbox", "read-only", "--cd", "{worktree}", "-"]
    )
    # Takes the Codex seat of a joint review while Codex has no quota (maintainer decision 2026-09-21). Empty: a
    # joint task waits for Codex instead. It must be read-only, like the reviewer it stands in for.
    codex_review_fallback: list[str] = field(default_factory=list)
    claude_review: list[str] = field(
        default_factory=lambda: [
            "claude", "-p", "--output-format", "text",
            "--tools", "Read,Grep,Glob",
            "--strict-mcp-config", "--disallowedTools", "mcp__*",  # no claude.ai connector tools
            "--permission-mode", "dontAsk",
        ]
    )
    # Which subscription each agent spends. Agents sharing a provider share one usage limit, so a limit hit by
    # the Claude reviewer also parks the Claude implementer. Override in `[agents.providers]` if a command changes.
    providers: dict[str, str] = field(default_factory=dict)

    def provider_of(self, agent: str) -> str:
        return self.providers.get(agent) or DEFAULT_PROVIDERS[agent]

    def implement_chain(self) -> list[Implementer]:
        """Who writes the code, in preference order. A fallback only covers the limits of the links before it."""
        chain = [Implementer(self.provider_of("implement"), self.implement)]
        chain += list(self.implement_fallbacks)
        if self.implement_fallback:
            chain.append(Implementer(self.provider_of("implement_fallback"), self.implement_fallback))
        return chain


@dataclass(frozen=True)
class Config:
    mode: str
    repo: Path
    tasks_file: Path
    state_dir: Path
    worktrees: Path
    base_branch: str
    port: int
    red_paths: list[str]
    network_modules: list[str]
    network_allowed_paths: list[str]  # files the plan allows to use network libraries (e.g. the LLM client)
    gates: list[Gate]
    timeouts: Timeouts
    limits: Limits
    agents: Agents


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value))


def _section(cls, raw: dict, name: str):
    """Build a settings dataclass, reporting an unknown or misspelled key as a config error, not a crash."""
    try:
        return cls(**raw)
    except TypeError as exc:
        raise ConfigError(f"[{name}]: {exc}") from exc


def _agents(raw: dict) -> Agents:
    raw = dict(raw)
    entries = raw.pop("implement_fallbacks", [])
    if entries and raw.get("implement_fallback"):
        raise ConfigError("use either implement_fallback or [[agents.implement_fallbacks]], not both")
    chain = []
    for index, entry in enumerate(entries, start=1):
        provider, cmd = entry.get("provider"), entry.get("cmd")
        if not isinstance(provider, str) or not provider or not isinstance(cmd, list) or not cmd:
            raise ConfigError(f"[[agents.implement_fallbacks]] #{index} needs a provider and a non-empty cmd")
        chain.append(Implementer(provider=provider, cmd=[str(part) for part in cmd]))
    return _section(Agents, {**raw, "implement_fallbacks": chain}, "agents")


def load_config(path: Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    mode = raw.get("mode", "dry-run")
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {MODES}, got {mode!r}")
    if "repo" not in raw:
        raise ConfigError("repo is required")
    if "tasks_file" not in raw:
        raise ConfigError("tasks_file is required")

    repo = _path(raw["repo"])
    gates_raw = raw.get(
        "gates",
        [
            {"name": "pytest", "cmd": ["python3", "-m", "pytest", "-q"]},
            {"name": "ruff-check", "cmd": ["python3", "-m", "ruff", "check", "."]},
            {"name": "ruff-format", "cmd": ["python3", "-m", "ruff", "format", "--check", "."]},
        ],
    )

    return Config(
        mode=mode,
        repo=repo,
        tasks_file=repo / raw["tasks_file"],
        state_dir=_path(raw.get("state_dir", "~/nightshift")),
        worktrees=_path(raw.get("worktrees", "~/work/wt")),
        base_branch=raw.get("base_branch", "develop"),
        port=int(raw.get("port", 8770)),
        red_paths=list(raw.get("red_paths", DEFAULT_RED_PATHS)),
        network_modules=list(raw.get("network_modules", DEFAULT_NETWORK_MODULES)),
        network_allowed_paths=list(raw.get("network_allowed_paths", [])),
        gates=[Gate(name=g["name"], cmd=list(g["cmd"])) for g in gates_raw],
        timeouts=_section(Timeouts, raw.get("timeouts", {}), "timeouts"),
        limits=_section(Limits, raw.get("limits", {}), "limits"),
        agents=_agents(raw.get("agents", {})),
    )
