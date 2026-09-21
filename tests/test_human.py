from nightshift.human import collect_answers, list_open, write_escalation
from nightshift.report import append_report


def test_escalation_file_round_trip(tmp_path):
    path = write_escalation(tmp_path, "SPK-01", kind="gate", reason="pytest failed twice",
                            branch="task/SPK-01-a1", run_id="r1")

    text = path.read_text(encoding="utf-8")
    assert "needs-human: SPK-01" in text and "type: gate" in text and "pytest failed twice" in text
    assert "decision:" in text
    assert [e["task_id"] for e in list_open(tmp_path)] == ["SPK-01"]
    assert collect_answers(tmp_path) == []  # unanswered files stay

    path.write_text(text.replace("decision:", "decision: A").replace("score:", "score: 4"), encoding="utf-8")
    answers = collect_answers(tmp_path)

    assert [(a.task_id, a.decision, a.score) for a in answers] == [("SPK-01", "retry", 4)]
    assert not path.exists()
    assert len(list((tmp_path / "answered").glob("SPK-01-*.md"))) == 1
    assert list_open(tmp_path) == []


def test_skip_and_invalid_answers(tmp_path):
    skip = write_escalation(tmp_path, "A", kind="review", reason="r", branch="b", run_id="r")
    skip.write_text(skip.read_text().replace("decision:", "decision: skip"))
    bad = write_escalation(tmp_path, "B", kind="review", reason="r", branch="b", run_id="r")
    bad.write_text(bad.read_text().replace("decision:", "decision: maybe"))

    answers = collect_answers(tmp_path)

    assert [(a.task_id, a.decision, a.score) for a in answers] == [("A", "skip", None)]
    assert bad.exists()


def test_report_appends_rows(tmp_path):
    append_report(tmp_path, "2026-09-17", run_id="r1", task_id="A", outcome="merged", detail="", seconds=61)
    append_report(tmp_path, "2026-09-17", run_id="r2", task_id="B", outcome="needs_human", detail="a|b", seconds=5)

    text = (tmp_path / "2026-09-17.md").read_text(encoding="utf-8")
    assert "| r1 |" in text and "| r2 |" in text and text.startswith("# Night shift report 2026-09-17")
    assert "a\\|b" in text
