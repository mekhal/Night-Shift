import http.client
import json
import threading

import pytest

from nightshift.dashboard import make_server
from nightshift.human import write_escalation
from nightshift.store import Store
from nightshift.tasks import Task


@pytest.fixture
def server(tmp_path):
    state = tmp_path / "state"
    store = Store(state / "nightshift.db")
    store.sync_tasks("plan.md", [Task("T-1", "Add <b>calc</b>", "green", "technical", ["src/**"], [], False, "b")])
    run_id = store.start_run("T-1")
    store.finish_run(run_id, "needs_human", "gate failed")
    store.set_state("mode", "on")
    store.set_state("current_task", "T-1")
    (state / "runs" / run_id).mkdir(parents=True)
    (state / "runs" / run_id / "implement.out").write_text("codex said hi")
    (tmp_path / "secret.txt").write_text("do not serve")
    write_escalation(state / "needs-human", "T-1", "gate", "pytest failed", "task/T-1-a1", run_id)

    srv = make_server(state, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, run_id
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, host=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_status_api(server):
    srv, run_id = server
    resp, body = request(srv, "GET", "/api/status")

    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("application/json")
    data = json.loads(body)
    assert data["state"]["mode"] == "on"
    assert data["state"]["current_task"] == "T-1"
    assert data["tasks"][0]["title"] == "Add <b>calc</b>"
    assert data["runs"][0]["run_id"] == run_id
    assert data["runs"][0]["files"] == ["implement.out"]
    assert data["needs_human"][0]["task_id"] == "T-1"
    assert "server_time" in data


def test_index_has_security_headers(server):
    srv, _ = server
    resp, body = request(srv, "GET", "/")

    assert resp.status == 200
    assert b"<title>" in body
    csp = resp.getheader("Content-Security-Policy")
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert b"innerHTML" not in body


def test_run_file_is_served_as_text(server):
    srv, run_id = server
    resp, body = request(srv, "GET", f"/runs/{run_id}/implement.out")

    assert resp.status == 200 and body == b"codex said hi"
    assert resp.getheader("Content-Type").startswith("text/plain")


def test_status_reports_each_provider_usage_limit(tmp_path):
    from nightshift.dashboard import status

    state = tmp_path / "state"
    store = Store(state / "nightshift.db")
    store.set_state("limit:codex:resume_after", "2026-09-18T12:00:00+00:00")
    store.set_state("limit:codex:message", "You've hit your usage limit.")
    store.set_state("limit:claude:resume_after", None)  # reset already collected

    limits = status(state)["limits"]

    assert limits == {"codex": {"resume_after": "2026-09-18T12:00:00+00:00",
                                "message": "You've hit your usage limit."}}


def test_status_lists_only_servable_files_and_survives_bad_run_rows(tmp_path):
    from nightshift.dashboard import status

    state = tmp_path / "state"
    store = Store(state / "nightshift.db")
    store.sync_tasks("p", [Task("T-1", "t", "green", "technical", ["src/**"], [], False, "b")])
    good = store.start_run("T-1")
    (state / "runs" / good).mkdir(parents=True)
    (state / "runs" / good / "ok.out").write_text("x")
    (state / "runs" / good / "bad name.out").write_text("x")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "leak.txt").write_text("x")
    store.conn.execute("INSERT INTO runs (run_id, task_id, started_at) VALUES ('../../outside', 'T-1', 'z')")

    data = status(state)

    files = {r["run_id"]: r["files"] for r in data["runs"]}
    assert files[good] == ["ok.out"]
    assert files["../../outside"] == []


def test_large_log_is_truncated_to_its_tail(server):
    srv, run_id = server
    from nightshift import dashboard

    path = srv.state_dir / "runs" / run_id / "big.out"
    path.write_bytes(b"a" * (dashboard.MAX_FILE_BYTES + 10) + b"END")
    resp, body = request(srv, "GET", f"/runs/{run_id}/big.out")

    assert resp.status == 200
    assert body.endswith(b"END") and len(body) < dashboard.MAX_FILE_BYTES + 200
    assert body.startswith(b"[truncated")


def test_needs_human_file_is_served_as_text(server):
    srv, _ = server
    resp, body = request(srv, "GET", "/needs-human/T-1.md")

    assert resp.status == 200 and b"pytest failed" in body
    assert resp.getheader("Content-Type").startswith("text/plain")


@pytest.mark.parametrize("path", ["/needs-human/../secret.txt", "/needs-human/nope.md", "/runs/../secret.txt", "/runs/x/../../secret.txt", "/runs/%2e%2e/%2e%2e/secret.txt",
                                  "/runs/nope/implement.out", "/nightshift.db", "/api/other"])
def test_other_paths_are_not_found(server, path):
    srv, _ = server
    resp, body = request(srv, "GET", path)

    assert resp.status == 404
    assert b"do not serve" not in body


def test_foreign_host_is_rejected(server):
    srv, _ = server
    resp, _ = request(srv, "GET", "/api/status", host="evil.example:8770")

    assert resp.status == 403


def test_localhost_host_is_accepted(server):
    srv, _ = server
    resp, _ = request(srv, "GET", "/api/status", host=f"localhost:{srv.server_address[1]}")

    assert resp.status == 200


def test_writes_are_not_allowed(server):
    srv, _ = server
    resp, _ = request(srv, "POST", "/api/status")

    assert resp.status == 405
