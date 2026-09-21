import fcntl
import json

from nightshift.__main__ import main
from nightshift.store import Store


def config(tmp_path, port=0):
    state = tmp_path / "state"
    path = tmp_path / "config.toml"
    path.write_text(f'mode = "off"\nrepo = "{tmp_path.as_posix()}"\ntasks_file = "t.toml"\n'
                    f'state_dir = "{state.as_posix()}"\nport = {port}\n')
    return path, state


def test_run_exits_when_another_instance_holds_the_lock(tmp_path, capsys):
    path, state = config(tmp_path)
    state.mkdir()
    with open(state / "nightshift.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert main(["run", "--config", str(path)]) == 0

    assert "already running" in capsys.readouterr().out


def test_run_stops_after_max_ticks(tmp_path):
    path, state = config(tmp_path)

    assert main(["run", "--config", str(path), "--max-ticks", "1"]) == 0
    assert Store(state / "nightshift.db").get_state("last_action") == "off"


def test_reset_breaker(tmp_path, capsys):
    path, state = config(tmp_path)
    store = Store(state / "nightshift.db")
    store.set_state("breaker_open", "1")
    store.set_state("consecutive_failures", "3")

    assert main(["reset-breaker", "--config", str(path)]) == 0

    assert store.get_state("breaker_open") == "0"
    assert store.get_state("consecutive_failures") == "0"


def test_mode_command_rewrites_only_the_mode_line(tmp_path, capsys):
    path, _ = config(tmp_path)
    path.write_text("# keep me\n" + path.read_text())

    assert main(["mode", "on-limited", "--config", str(path)]) == 0
    text = path.read_text()
    assert 'mode = "on-limited"' in text and "# keep me" in text

    assert main(["mode", "turbo", "--config", str(path)]) == 2
    assert 'mode = "on-limited"' in path.read_text()


def test_answer_command_fills_the_escalation(tmp_path, capsys):
    from nightshift.human import collect_answers, write_escalation

    path, state = config(tmp_path)
    write_escalation(state / "needs-human", "T-1", "gate", "pytest failed", "task/T-1-a1", "r1")

    assert main(["show", "T-1", "--config", str(path)]) == 0
    assert "pytest failed" in capsys.readouterr().out

    assert main(["answer", "T-1", "retry", "--score", "4", "--config", str(path)]) == 0
    answers = collect_answers(state / "needs-human")
    assert [(a.task_id, a.decision, a.score) for a in answers] == [("T-1", "retry", 4)]

    assert main(["answer", "NOPE", "skip", "--config", str(path)]) == 1


def test_status_prints_json(tmp_path, capsys):
    path, state = config(tmp_path)
    Store(state / "nightshift.db").set_state("mode", "off")

    assert main(["status", "--config", str(path)]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["state"]["mode"] == "off"
