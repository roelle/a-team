"""The admin page's API: agents, norms, memories, channels for everyone,
graceful restart, and Update -- end to end over HTTP."""
import json
import os
import time

from test_api import call, server, team  # noqa: F401  (fixtures)


def test_agents_can_be_added_changed_and_removed(server):
    app, port, td, _ = server
    _, r = call(port, "POST", "/api/agent", {"team": "demo", "action": "add", "name": "at-Ada",
                                             "role": "researches", "model": "pro"}, token=app.token)
    assert r["name"] == "at-Ada" and r["channels"] == ["general"]
    call(port, "POST", "/api/agent", {"team": "demo", "action": "update", "name": "at-Ada",
                                      "role": "researches and writes", "tools_off": "run_command",
                                      "is_principal": False}, token=app.token)
    _, a = call(port, "GET", "/api/admin?team=demo", token=app.token)
    ada = next(x for x in a["agents"] if x["name"] == "at-Ada")
    assert ada == {"name": "at-Ada", "role": "researches and writes", "model": "pro",
                   "tools_off": ["run_command"]}
    status, err = call(port, "POST", "/api/agent", {"team": "demo", "action": "add", "name": "user"},
                       token=app.token)
    assert status == 400 and "cannot be an agent's name" in err["error"]
    call(port, "POST", "/api/agent", {"team": "demo", "action": "remove", "name": "at-Ada"}, token=app.token)
    _, a = call(port, "GET", "/api/admin?team=demo", token=app.token)
    assert "at-Ada" not in [x["name"] for x in a["agents"]]


def test_norms_and_memories(server):
    app, port, td, _ = server
    call(port, "POST", "/api/norms", {"team": "demo", "text": "- Review first."}, token=app.token)
    assert (td / "NORMS.md").read_text() == "- Review first.\n"
    _, a = call(port, "GET", "/api/admin?team=demo", token=app.token)
    assert a["norms"] == "- Review first.\n"
    call(port, "POST", "/api/memory", {"team": "demo", "action": "save", "agent": "coder",
                                       "name": "Build", "description": "how to build",
                                       "content": "make all"}, token=app.token)
    _, m = call(port, "GET", "/api/memories?team=demo&agent=coder", token=app.token)
    assert [x["name"] for x in m["memories"]] == ["build"] and "make all" in m["memories"][0]["content"]
    call(port, "POST", "/api/memory", {"team": "demo", "action": "delete", "agent": "coder",
                                       "name": "build"}, token=app.token)
    _, m = call(port, "GET", "/api/memories?team=demo&agent=coder", token=app.token)
    assert m["memories"] == []
    status, _ = call(port, "GET", "/api/memories?team=demo&agent=nobody", token=app.token)
    assert status == 400


def test_a_channel_for_everyone_takes_in_agents_added_later(server):
    app, port, td, _ = server
    call(port, "POST", "/api/channel", {"team": "demo", "action": "create", "name": "all-hands",
                                        "everyone": True}, token=app.token)
    call(port, "POST", "/api/agent", {"team": "demo", "action": "add", "name": "ops",
                                      "all_channels": False}, token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert "ops" in st["channels"]["all-hands"]["members"] and st["channels"]["all-hands"]["everyone"]


def test_restart_is_refused_without_a_supervisor_that_knows_it(server):
    app, port, td, _ = server
    status, err = call(port, "POST", "/api/supervisor", {"team": "demo", "action": "restart"},
                       token=app.token)
    assert status == 400 and "no supervisor" in err["error"]
    (td / "supervisor.json").write_text(json.dumps({
        "pid": os.getpid(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "restartable": True, "inflight": ["coder"]}))
    status, r = call(port, "POST", "/api/supervisor", {"team": "demo", "action": "restart"},
                     token=app.token)
    assert status == 200 and r["requested"] and json.loads((td / ".restart").read_text())["by"] == "matt"
    _, v = call(port, "GET", "/api/versions", token=app.token)
    assert v["teams"][0]["running"] and v["teams"][0]["inflight"] == ["coder"]


def test_update_runs_in_the_background_then_restarts_the_app(server, monkeypatch):
    app, port, td, _ = server
    from agychat import update as up
    calls = []
    monkeypatch.setattr(up, "run", lambda dirs, now=False, log=print: (
        calls.append((list(dirs), now)), log("updated"), {"teams": []})[-1])
    restarted = []
    app.reexec = lambda: restarted.append(True)
    _, r = call(port, "POST", "/api/update", {"team": "demo", "action": "run", "now": True},
                token=app.token)
    assert r["started"]
    for _ in range(50):
        if restarted:
            break
        time.sleep(0.1)
    assert restarted and calls == [([td.resolve()], True)]
    _, v = call(port, "GET", "/api/versions", token=app.token)
    assert v["update"]["state"] == "restarting" and "updated" in v["log"]


def test_the_token_survives_a_restart_and_is_not_left_for_supervisors(monkeypatch, tmp_path):
    from agychat.server import App
    monkeypatch.setenv("AGYCHAT_TOKEN", "kept-token")
    app = App(tmp_path, [], "matt")
    assert app.token == "kept-token" and "AGYCHAT_TOKEN" not in os.environ


def test_the_admin_page_is_served(server):
    import http.client
    app, port, *_ = server
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", f"/admin?token={app.token}", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    assert r.status == 200 and b"agy<span>admin" in r.read()


def test_memories_come_with_their_history_and_an_archive_to_restore_from(server):
    app, port, td, _ = server
    for content in ("make all", "make all; make test"):
        call(port, "POST", "/api/memory", {"team": "demo", "action": "save", "agent": "coder",
                                           "name": "build", "content": content}, token=app.token)
    _, m = call(port, "GET", "/api/memories?team=demo&agent=coder", token=app.token)
    [b] = m["memories"]
    assert b["revisions"] == 2 and b["reads"] == 0 and b["created"] and "make test" in b["content"]
    call(port, "POST", "/api/memory", {"team": "demo", "action": "delete", "agent": "coder",
                                       "name": "build"}, token=app.token)
    _, m = call(port, "GET", "/api/memories?team=demo&agent=coder", token=app.token)
    assert m["memories"] == [] and [a["name"] for a in m["archived"]] == ["build"]
    call(port, "POST", "/api/memory", {"team": "demo", "action": "restore", "agent": "coder",
                                       "id": m["archived"][0]["id"]}, token=app.token)
    _, m = call(port, "GET", "/api/memories?team=demo&agent=coder", token=app.token)
    assert [x["name"] for x in m["memories"]] == ["build"] and m["archived"] == []


def test_a_review_is_queued_only_with_a_supervisor_running(server):
    app, port, td, _ = server
    call(port, "POST", "/api/memory", {"team": "demo", "action": "save", "agent": "qa",
                                       "name": "x", "content": "y"}, token=app.token)
    status, err = call(port, "POST", "/api/memory", {"team": "demo", "action": "review",
                                                     "agent": "qa"}, token=app.token)
    assert status == 400 and "no supervisor" in err["error"]
    (td / "supervisor.json").write_text(json.dumps({
        "pid": os.getpid(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    status, r = call(port, "POST", "/api/memory", {"team": "demo", "action": "review",
                                                   "agent": "qa"}, token=app.token)
    assert status == 200 and r["queued"]
    _, m = call(port, "GET", "/api/memories?team=demo&agent=qa", token=app.token)
    assert m["review_queued"]


def test_lessons_in_common_are_offered_as_norm_candidates(server):
    app, port, *_ = server
    lesson = ("Drive the real code path in review: a test against a static mock passed while "
              "the deployed binary failed, so run the actual binary end to end before approving")
    for agent in ("tpm", "coder", "qa"):
        call(port, "POST", "/api/memory", {"team": "demo", "action": "save", "agent": agent,
                                           "name": f"{agent}-real-path", "content": lesson,
                                           "description": "drive the real path"}, token=app.token)
    _, r = call(port, "GET", "/api/norm_candidates?team=demo", token=app.token)
    assert len(r["groups"]) == 1 and r["groups"][0]["agents"] == ["coder", "qa", "tpm"]


def test_the_favicon_is_served_without_the_token(server):
    import http.client
    app, port, *_ = server
    for path in ("/favicon.ico", "/static/favicon.svg"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Content-Type") == "image/svg+xml" \
            and r.read().startswith(b"<svg")


def test_memories_come_with_how_recall_has_gone(server):
    app, port, td, _ = server
    import time
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with (td / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": now, "event": "recall", "agent": "coder", "picked": ["build"], "hints": []}) + "\n")
        f.write(json.dumps({"ts": now, "event": "recall_miss", "agent": "coder", "memory": "build",
                            "correction": "run make", "learned": ["make"]}) + "\n")
    _, m = call(port, "GET", "/api/memories?team=demo&agent=coder", token=app.token)
    r = m["recall"]
    assert r["enabled"] is False and r["days"][-1]["wakes"] == 1 and r["days"][-1]["misses"] == 1
    assert r["memories"]["build"] == {"offered": 1, "hinted": 0, "missed": 1, "last_offered": now}
