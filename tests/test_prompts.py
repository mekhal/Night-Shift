from nightshift.config import DEFAULT_NETWORK_MODULES, DEFAULT_RED_PATHS
from nightshift.prompts import fix_prompt, implement_prompt
from nightshift.tasks import Task


def task(paths=("src/**", "tests/**")):
    return Task(id="T", title="t", tier="green", review="technical", paths=list(paths),
                depends_on=[], no_tests=False, body="b")


def rules_for(t, network_allowed_paths=()):
    return implement_prompt(t, [], DEFAULT_RED_PATHS, DEFAULT_NETWORK_MODULES, network_allowed_paths)


def test_human_only_paths_are_listed_from_the_configuration():
    text = rules_for(task())

    assert "README.md" in text
    assert "docs/superpowers/**" in text


def test_a_red_path_the_task_names_literally_is_allowed_and_said_so():
    # DIAG-01 (2026-09-20): the task named README.md, the gate allowed it, the rules still said "never touch
    # README.md", so the implementer stopped and wrote nothing.
    text = rules_for(task(paths=("src/**", "tests/**", "README.md")))

    assert "you may change README.md" in text
    assert "Never touch" not in text


def test_a_wildcard_does_not_claim_a_red_path_is_allowed():
    text = rules_for(task(paths=("**",)))

    assert "you may change" not in text


def test_network_libraries_stay_forbidden_when_the_task_has_no_exception():
    text = rules_for(task())

    assert "Do not add network access" in text
    assert "where the plan allows" not in text


def test_a_file_the_plan_allows_network_in_is_named_when_the_task_may_touch_it():
    text = rules_for(task(paths=("src/privasheet/selfcheck.py", "tests/**")),
                     network_allowed_paths=("src/privasheet/selfcheck.py", "src/privasheet/llm.py"))

    assert "src/privasheet/selfcheck.py" in text.split("Do not add network access")[1].splitlines()[0]
    assert "src/privasheet/llm.py" not in text.split("Do not add network access")[1].splitlines()[0]


def test_the_fix_prompt_carries_the_same_rules():
    t = task(paths=("src/**", "tests/**", "README.md"))

    assert "you may change README.md" in fix_prompt(t, ["boom"], [], DEFAULT_RED_PATHS,
                                                    DEFAULT_NETWORK_MODULES, ())
