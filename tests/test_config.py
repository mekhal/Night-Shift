from pathlib import Path

import pytest

from nightshift.config import ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_uses_defaults(tmp_path):
    cfg = load_config(write(tmp_path, 'repo = "/work/repo"\ntasks_file = "plan.tasks.toml"\n'))

    assert cfg.mode == "dry-run"
    assert cfg.repo == Path("/work/repo")
    assert cfg.tasks_file == Path("/work/repo/plan.tasks.toml")
    assert cfg.base_branch == "develop"
    assert cfg.port == 8770
    assert cfg.timeouts.implement == 2700
    assert cfg.limits.max_attempts == 3
    assert cfg.limits.max_diff_lines == 400
    assert "tools/nightshift/**" not in cfg.red_paths  # orchestrator lives outside the repo
    assert ".github/**" in cfg.red_paths
    assert [g.name for g in cfg.gates] == ["pytest", "ruff-check", "ruff-format"]
    assert cfg.network_allowed_paths == []


def test_claude_reviewer_has_no_mcp_connector_tools_by_default(tmp_path):
    # Phase 0: --tools alone left claude.ai connector tools (e.g. Claude Docs create/delete) available.
    cfg = load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n'))

    assert "--strict-mcp-config" in cfg.agents.claude_review
    assert cfg.agents.claude_review[cfg.agents.claude_review.index("--disallowedTools") + 1] == "mcp__*"


def test_no_implement_fallback_unless_one_is_configured(tmp_path):
    cfg = load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n'))

    assert cfg.agents.implement_fallback == []
    assert cfg.agents.provider_of("implement") == "codex"
    assert cfg.agents.provider_of("implement_fallback") == "claude"
    assert cfg.limits.quota_poll_s == 300


def test_shipped_example_config_codes_with_gpt_then_gemini_then_sonnet(tmp_path):
    # Maintainer decision 2026-09-21: gpt-5.5 (codex) > gemini-3.7-flash > claude sonnet, models spelled out.
    example = (Path(__file__).parent.parent / "config.example.toml").read_text(encoding="utf-8")

    cfg = load_config(write(tmp_path, example))
    chain = cfg.agents.implement_chain()

    def model(cmd):
        flag = "--model" if "--model" in cmd else "-m"
        return cmd[cmd.index(flag) + 1]

    assert [i.provider for i in chain] == ["codex", "gemini", "claude"]
    assert [model(i.cmd) for i in chain] == ["gpt-5.5", "gemini-3.7-flash", "sonnet"]
    assert cfg.agents.provider_of("claude_review") == "claude"  # same subscription, so one shared limit


def test_the_shipped_codex_review_stand_in_is_read_only_sonnet(tmp_path):
    example = (Path(__file__).parent.parent / "config.example.toml").read_text(encoding="utf-8")

    cmd = load_config(write(tmp_path, example)).agents.codex_review_fallback

    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,Bash"
    assert "bypassPermissions" not in cmd


def test_the_shipped_reviewer_is_claude_opus(tmp_path):
    # The reviewer model is spelled out so a change to the CLI default cannot quietly downgrade reviews.
    example = (Path(__file__).parent.parent / "config.example.toml").read_text(encoding="utf-8")

    review = load_config(write(tmp_path, example)).agents.claude_review

    assert review[review.index("--model") + 1] == "opus"


def test_a_provider_override_beats_the_default(tmp_path):
    cfg = load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n\n'
                                      '[agents.providers]\nimplement = "claude"\n'))

    assert cfg.agents.provider_of("implement") == "claude"
    assert cfg.agents.provider_of("codex_review") == "codex"


def test_mode_is_validated(tmp_path):
    with pytest.raises(ConfigError, match="mode"):
        load_config(write(tmp_path, 'mode = "yes"\nrepo = "/r"\ntasks_file = "t.toml"\n'))


def test_repo_is_required(tmp_path):
    with pytest.raises(ConfigError, match="repo"):
        load_config(write(tmp_path, 'tasks_file = "t.toml"\n'))


def test_home_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(write(tmp_path, 'repo = "~/work/r"\ntasks_file = "t.toml"\nstate_dir = "~/ns"\n'))

    assert cfg.repo == tmp_path / "work" / "r"
    assert cfg.state_dir == tmp_path / "ns"


def test_gates_and_agent_commands_can_be_overridden(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
repo = "/r"
tasks_file = "t.toml"

[[gates]]
name = "unit"
cmd = ["pytest", "-q"]

[agents]
implement = ["fake-codex", "{worktree}"]
""",
        )
    )

    assert [(g.name, g.cmd) for g in cfg.gates] == [("unit", ["pytest", "-q"])]
    assert cfg.agents.implement == ["fake-codex", "{worktree}"]
    assert cfg.agents.claude_review[0] == "claude"


def test_a_fallback_chain_is_tried_in_order(tmp_path):
    cfg = load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n\n'
                                      '[[agents.implement_fallbacks]]\n'
                                      'provider = "gemini"\ncmd = ["gemini", "--yolo"]\n\n'
                                      '[[agents.implement_fallbacks]]\n'
                                      'provider = "claude"\ncmd = ["claude", "-p"]\n'))

    assert [i.provider for i in cfg.agents.implement_chain()] == ["codex", "gemini", "claude"]
    assert cfg.agents.implement_chain()[1].cmd == ["gemini", "--yolo"]


def test_one_fallback_may_still_be_written_the_short_way(tmp_path):
    cfg = load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n\n'
                                      '[agents]\nimplement_fallback = ["claude", "-p"]\n'))

    assert [i.provider for i in cfg.agents.implement_chain()] == ["codex", "claude"]


def test_the_two_ways_of_writing_a_fallback_cannot_be_mixed(tmp_path):
    with pytest.raises(ConfigError, match="implement_fallbacks"):
        load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n\n'
                                    '[agents]\nimplement_fallback = ["claude"]\n\n'
                                    '[[agents.implement_fallbacks]]\n'
                                    'provider = "gemini"\ncmd = ["gemini"]\n'))


def test_a_fallback_without_a_provider_or_command_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n\n'
                                    '[[agents.implement_fallbacks]]\ncmd = ["gemini"]\n'))


def test_a_misspelled_setting_is_a_config_error_not_a_crash(tmp_path):
    # 2026-09-19: an unknown [limits] key raised TypeError inside the loop, which read as a crash on the dashboard.
    with pytest.raises(ConfigError, match="limits"):
        load_config(write(tmp_path, 'repo = "/r"\ntasks_file = "t.toml"\n\n[limits]\nmax_attemps = 3\n'))
