from nightshift.store import Store
from nightshift.tasks import Task


def task(tid, tier="green", depends_on=(), title="t", body="b"):
    return Task(
        id=tid, title=title, tier=tier, review="technical", paths=["src/**"],
        depends_on=list(depends_on), no_tests=False, body=body,
    )


def test_sync_inserts_new_tasks_as_ready(tmp_path):
    store = Store(tmp_path / "ns.db")
    store.sync_tasks("plan.md", [task("A"), task("B")])

    rows = store.list_tasks()
    assert [(r["task_id"], r["status"], r["attempts"]) for r in rows] == [("A", "ready", 0), ("B", "ready", 0)]
    assert rows[0]["plan"] == "plan.md"


def test_sync_updates_definition_but_keeps_progress(tmp_path):
    store = Store(tmp_path / "ns.db")
    store.sync_tasks("plan.md", [task("A", title="old")])
    store.set_status("A", "merged")

    store.sync_tasks("plan.md", [task("A", title="new")])

    row = store.get_task("A")
    assert row["title"] == "new"
    assert row["status"] == "merged"


def test_next_eligible_respects_order_tier_and_dependencies(tmp_path):
    store = Store(tmp_path / "ns.db")
    store.sync_tasks("p", [task("A"), task("B", depends_on=["A"]), task("C", tier="yellow"), task("D")])

    assert store.next_eligible({"green"}).id == "A"

    store.set_status("A", "needs_human")
    assert store.next_eligible({"green"}).id == "D"  # B waits for A, C is yellow

    store.set_status("A", "merged")
    assert store.next_eligible({"green"}).id == "B"
    assert store.next_eligible({"green", "yellow"}).id == "B"

    store.set_status("B", "merged")
    store.set_status("D", "merged")
    assert store.next_eligible({"green"}) is None
    assert store.next_eligible({"green", "yellow"}).id == "C"


def test_attempts_step_and_state(tmp_path):
    store = Store(tmp_path / "ns.db")
    store.sync_tasks("p", [task("A")])

    assert store.increment_attempts("A") == 1
    assert store.increment_attempts("A") == 2
    store.set_step("A", "gates")
    assert store.get_task("A")["step"] == "gates"

    assert store.get_state("resume_after") is None
    store.set_state("resume_after", "2026-09-17T15:00:00+00:00")
    assert Store(tmp_path / "ns.db").get_state("resume_after") == "2026-09-17T15:00:00+00:00"
    store.set_state("resume_after", None)
    assert store.get_state("resume_after") is None


def test_runs_are_recorded_newest_first(tmp_path):
    store = Store(tmp_path / "ns.db")
    store.sync_tasks("p", [task("A")])

    first = store.start_run("A")
    store.finish_run(first, "needs_human", "gate: pytest")
    second = store.start_run("A")
    store.finish_run(second, "merged", "")

    runs = store.recent_runs(10)
    assert [r["run_id"] for r in runs] == [second, first]
    assert runs[1]["outcome"] == "needs_human" and runs[1]["detail"] == "gate: pytest"
    assert store.get_task("A")["last_run"] == second
