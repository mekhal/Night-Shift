import pytest

from nightshift.tasks import TaskFileError, load_tasks

VALID = '''
plan = "docs/superpowers/plans/2026-09-18-spike.md"

[[task]]
id = "SPK-01"
title = "Synthetic invoice generator"
tier = "green"
review = "technical"
paths = ["spike/generator/**", "spike/tests/test_generator.py"]
body = """
Generate PNG invoices with Pillow.
Acceptance: 2 layouts, deterministic with a seed.
"""

[[task]]
id = "SPK-02"
title = "Benchmark dashboard mockup"
tier = "yellow"
review = "joint"
paths = ["spike/report/**"]
depends_on = ["SPK-01"]
no_tests = true
body = "HTML report."
'''


def test_load_tasks_in_file_order(tmp_path):
    path = tmp_path / "plan.tasks.toml"
    path.write_text(VALID, encoding="utf-8")

    plan, tasks = load_tasks(path)

    assert plan == "docs/superpowers/plans/2026-09-18-spike.md"
    assert [t.id for t in tasks] == ["SPK-01", "SPK-02"]
    first, second = tasks
    assert first.tier == "green" and first.review == "technical"
    assert first.depends_on == [] and first.no_tests is False
    assert "Acceptance" in first.body
    assert second.depends_on == ["SPK-01"] and second.no_tests is True
    assert second.review == "joint"


@pytest.mark.parametrize(
    "bad, message",
    [
        ('[[task]]\nid="A"\ntitle="t"\ntier="blue"\nreview="technical"\npaths=["a"]\nbody="b"\n', "tier"),
        ('[[task]]\nid="A"\ntitle="t"\ntier="green"\nreview="solo"\npaths=["a"]\nbody="b"\n', "review"),
        ('[[task]]\nid="A"\ntitle="t"\ntier="green"\nreview="technical"\npaths=[]\nbody="b"\n', "paths"),
        ('[[task]]\ntitle="t"\ntier="green"\nreview="technical"\npaths=["a"]\nbody="b"\n', "id"),
        ('[[task]]\nid="A; rm -rf ~"\ntitle="t"\ntier="green"\nreview="technical"\npaths=["a"]\nbody="b"\n', "id"),
        ('[[task]]\nid="../x"\ntitle="t"\ntier="green"\nreview="technical"\npaths=["a"]\nbody="b"\n', "id"),
        (
            '[[task]]\nid="A"\ntitle="t"\ntier="green"\nreview="technical"\npaths=["a"]\nbody="b"\n'
            '[[task]]\nid="A"\ntitle="t"\ntier="green"\nreview="technical"\npaths=["a"]\nbody="b"\n',
            "duplicate",
        ),
        (
            '[[task]]\nid="A"\ntitle="t"\ntier="green"\nreview="technical"\npaths=["a"]\nbody="b"\ndepends_on=["Z"]\n',
            "unknown",
        ),
    ],
)
def test_invalid_task_files_are_rejected(tmp_path, bad, message):
    path = tmp_path / "t.toml"
    path.write_text('plan = "p.md"\n' + bad, encoding="utf-8")

    with pytest.raises(TaskFileError, match=message):
        load_tasks(path)
