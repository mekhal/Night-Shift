import sys

from nightshift.config import DEFAULT_NETWORK_MODULES, DEFAULT_RED_PATHS, Gate
from nightshift.gates import check_diff, parse_diff, run_command_gates
from nightshift.tasks import Task


def task(paths=("src/**", "tests/**"), no_tests=False):
    return Task(id="T", title="t", tier="green", review="technical", paths=list(paths),
                depends_on=[], no_tests=no_tests, body="b")


def diff_for(files: dict[str, tuple[str, list[str], list[str]]]) -> str:
    """files: path -> (kind, removed, added); kind is 'new', 'deleted' or 'modified'."""
    chunks = []
    for path, (kind, removed, added) in files.items():
        chunks.append(f"diff --git a/{path} b/{path}")
        if kind == "new":
            chunks += ["new file mode 100644", "--- /dev/null", f"+++ b/{path}"]
        elif kind == "deleted":
            chunks += ["deleted file mode 100644", f"--- a/{path}", "+++ /dev/null"]
        else:
            chunks += [f"--- a/{path}", f"+++ b/{path}"]
        chunks.append(f"@@ -1,{len(removed)} +1,{len(added)} @@")
        chunks += ["-" + line for line in removed] + ["+" + line for line in added]
    return "\n".join(chunks) + "\n"


def failures(diff_text, t=None, max_lines=400):
    parsed = parse_diff(diff_text)
    return {f.gate for f in check_diff(parsed, t or task(), DEFAULT_RED_PATHS, DEFAULT_NETWORK_MODULES, max_lines)}


GOOD = {
    "src/calc.py": ("modified", ["def add(a, b):", "    return a - b"], ["def add(a, b):", "    return a + b"]),
    "tests/test_calc.py": ("new", [], ["def test_add():", "    assert add(1, 2) == 3"]),
}


def test_parse_diff_counts_lines_and_kinds():
    parsed = parse_diff(diff_for(GOOD))

    assert parsed["src/calc.py"].added == ["def add(a, b):", "    return a + b"]
    assert parsed["src/calc.py"].removed == ["def add(a, b):", "    return a - b"]
    assert parsed["tests/test_calc.py"].kind == "new"


def test_good_change_passes():
    assert failures(diff_for(GOOD)) == set()


def test_empty_diff_fails():
    assert failures("") == {"no-changes"}


def test_out_of_scope_and_red_paths():
    files = dict(GOOD)
    files["docs/notes.md"] = ("new", [], ["hello"])
    files[".github/workflows/ci.yml"] = ("modified", ["a"], ["b"])

    assert failures(diff_for(files)) == {"scope", "red-path"}


def test_size_limit():
    files = dict(GOOD)
    files["src/big.py"] = ("new", [], [f"x{i} = {i}" for i in range(50)])

    assert "size" in failures(diff_for(files), max_lines=40)


def test_tests_required_unless_no_tests():
    only_code = {"src/calc.py": GOOD["src/calc.py"]}

    assert failures(diff_for(only_code)) == {"new-tests"}
    assert failures(diff_for(only_code), task(no_tests=True)) == set()


def test_removed_or_skipped_tests_fail():
    removed = dict(GOOD)
    removed["tests/test_old.py"] = ("modified", ["def test_old():", "    assert True"], [])
    assert "tests-weakened" in failures(diff_for(removed))

    skipped = dict(GOOD)
    skipped["tests/test_calc.py"] = ("new", [], ["import pytest", "@pytest.mark.skip", "def test_add():", "  pass"])
    assert "tests-weakened" in failures(diff_for(skipped))

    deleted = dict(GOOD)
    deleted["tests/test_gone.py"] = ("deleted", ["def test_gone():", "    pass"], [])
    assert "tests-weakened" in failures(diff_for(deleted))


def test_renamed_test_function_is_not_weakening():
    files = {
        "src/calc.py": GOOD["src/calc.py"],
        "tests/test_calc.py": ("modified", ["def test_add():"], ["def test_add():", "def test_add_negative():"]),
    }
    assert failures(diff_for(files)) == set()


def test_network_import_and_secret_fail():
    files = dict(GOOD)
    files["src/calc.py"] = ("modified", [], ["import requests", "from http.client import HTTPConnection"])
    files["src/keys.py"] = ("new", [], ['TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"'])

    assert failures(diff_for(files)) == {"network", "secrets"}


def test_network_import_allowed_only_in_configured_paths():
    files = dict(GOOD)
    files["src/llm.py"] = ("new", [], ["import http.client"])
    files["tests/test_llm.py"] = ("new", [], ["import socket", "def test_x():", "    pass"])
    files["src/other.py"] = ("new", [], ["import socket"])
    parsed = parse_diff(diff_for(files))

    result = check_diff(parsed, task(), DEFAULT_RED_PATHS, DEFAULT_NETWORK_MODULES, 400,
                        network_allowed_paths=["src/llm.py", "tests/test_llm.py"])

    network = [f for f in result if f.gate == "network"]
    assert len(network) == 1 and "src/other.py" in network[0].detail and "llm" not in network[0].detail


def test_command_gates_report_failures_with_output(tmp_path):
    gates = [
        Gate("ok", [sys.executable, "-c", "print('fine')"]),
        Gate("bad", [sys.executable, "-c", "import sys; print('boom'); sys.exit(1)"]),
    ]

    result = run_command_gates(gates, tmp_path, timeout=30)

    assert [f.gate for f in result] == ["bad"]
    assert "boom" in result[0].detail


def test_a_task_that_names_a_red_path_literally_may_change_it():
    # DEP-01 (2026-09-19): the task said pyproject.toml and README.md; only the maintainer writes a task file,
    # so naming the file there is the human decision the red-path rule asks for.
    files = dict(GOOD)
    files["pyproject.toml"] = ("modified", ["deps = []"], ['deps = ["rapidocr"]'])

    assert failures(diff_for(files), task(paths=("src/**", "tests/**", "pyproject.toml"))) == set()


def test_a_wildcard_never_unlocks_a_red_path():
    files = dict(GOOD)
    files["pyproject.toml"] = ("modified", ["deps = []"], ['deps = ["rapidocr"]'])

    assert failures(diff_for(files), task(paths=("**",))) == {"red-path"}
