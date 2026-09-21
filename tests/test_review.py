import json

from nightshift.config import Gate
from nightshift.review import Verdict, combine, parse_verdict
from nightshift.prompts import implement_prompt, fix_prompt, review_prompt
from nightshift.tasks import Task

GATES = [Gate(name="ruff-format", cmd=["/venv/bin/ruff", "format", "--check", "."])]

TASK = Task(id="SPK-01", title="Generator", tier="green", review="technical",
            paths=["spike/**"], depends_on=[], no_tests=False, body="Make invoices.")


def verdict_json(verdict="approve", **extra):
    return json.dumps({"verdict": verdict, "risk": "green", "findings": [], "reason": "ok", **extra})


def test_parse_plain_json():
    v = parse_verdict(verdict_json("approve"))

    assert v == Verdict("approve", "green", [], "ok")


def test_parse_takes_last_json_block_in_chatty_output():
    text = (
        'Looking at {"verdict": "changes"} in the notes...\n'
        "```json\n" + verdict_json("changes", findings=[
            {"severity": "high", "file": "spike/a.py", "line": 3, "issue": "bug", "fix": "fix it"}
        ]) + "\n```\nDone."
    )
    v = parse_verdict(text)

    assert v.verdict == "changes"
    assert v.findings[0]["issue"] == "bug"


def test_invalid_output_escalates():
    assert parse_verdict("I think it looks fine").verdict == "escalate"
    assert parse_verdict('{"verdict": "ship it"}').verdict == "escalate"
    assert parse_verdict('{"verdict": "approve", "risk": "green", "findings": "none"}').verdict == "escalate"


def test_approve_with_red_risk_escalates():
    assert parse_verdict(verdict_json("approve", risk="red")).verdict == "escalate"


def test_combine_joint_reviews():
    approve = Verdict("approve", "green", [], "ok")
    changes = Verdict("changes", "green", [{"issue": "a"}], "fix")
    changes2 = Verdict("changes", "yellow", [{"issue": "b"}], "fix too")
    escalate = Verdict("escalate", "yellow", [], "unsure")

    assert combine({"claude": approve}) == approve
    assert combine({"claude": approve, "codex": approve}).verdict == "approve"
    both = combine({"claude": changes, "codex": changes2})
    assert both.verdict == "changes" and len(both.findings) == 2 and both.risk == "yellow"
    assert combine({"claude": approve, "codex": escalate}).verdict == "escalate"
    split = combine({"claude": approve, "codex": changes})
    assert split.verdict == "escalate" and "disagree" in split.reason


def test_prompts_carry_task_and_rules():
    impl = implement_prompt(TASK)
    assert "SPK-01" in impl and "Make invoices." in impl and "spike/**" in impl
    assert "test" in impl.lower() and "git commit" in impl

    fix = fix_prompt(TASK, ["pytest: 1 failed"])
    assert "pytest: 1 failed" in fix


def test_prompts_list_the_gate_commands_the_work_is_judged_by():
    command = "/venv/bin/ruff format --check ."
    assert command in implement_prompt(TASK, GATES)
    assert command in fix_prompt(TASK, ["gate ruff-format: unformatted"], GATES)
    assert command not in implement_prompt(TASK)  # no gates configured, no gates section

    rev = review_prompt(TASK, "diff --git a/x b/x", "all gates passed")
    assert "diff --git a/x b/x" in rev and '"verdict"' in rev and "escalate" in rev
