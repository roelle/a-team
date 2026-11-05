"""The server, end to end over HTTP, against a real (temporary) team directory."""
import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from ateam.chat.server import App, serve
from ateam.transport_file import FileTransport


@pytest.fixture
def team(tmp_path, monkeypatch):
    root = tmp_path / "teams"
    td = root / "demo" / "team"
    (td / "inbox").mkdir(parents=True)
    ws = tmp_path / "work"
    ws.mkdir()
    (ws / "report.md").write_text("# Findings\n\n- allocate drops a cent\n")
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "tpm", "role": "coordinates"},
        {"name": "coder", "role": "implements", "model": "flash", "workspaces": [str(ws)]},
        {"name": "qa", "role": "reviews", "model": "pro"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.delenv("ATEAM_BUS_TRANSPORT", raising=False)
    return root, td, ws


@pytest.fixture
def server(team):
    root, td, ws = team
    app = App(root, [], "matt")
    httpd = serve(app, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield app, httpd.server_address[1], td, ws
    httpd.shutdown()


def call(port, method, path, body=None, token=None, host="127.0.0.1", ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": f"{host}:{port}"}
    if token:
        headers["X-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = ctype
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode(errors="replace")


# --- who may talk to it --------------------------------------------------------

def test_the_api_needs_the_token(server):
    app, port, *_ = server
    assert call(port, "GET", "/api/teams")[0] == 401
    status, body = call(port, "GET", "/api/teams", token=app.token)
    assert status == 200 and [t["name"] for t in body["teams"]] == ["demo"]


def test_a_page_from_another_host_name_is_refused(server):
    app, port, *_ = server
    assert call(port, "GET", "/api/teams", token=app.token, host="evil.example")[0] == 403


def test_a_change_must_be_json(server):
    app, port, *_ = server
    status, _ = call(port, "POST", "/api/post", {"team": "demo", "channel": "general",
                                                 "content": "hi"}, token=app.token, ctype="text/plain")
    assert status == 415


def test_the_page_sets_the_cookie_from_the_token(server):
    app, port, *_ = server
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", f"/?token={app.token}", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    r.read()
    assert r.status == 200 and app.token in (r.getheader("Set-Cookie") or "")
    assert call(port, "GET", "/")[0] == 401


# --- what it shows ---------------------------------------------------------------

def test_state_has_agents_models_presence_and_channels(server):
    app, port, *_ = server
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert [a["name"] for a in st["agents"]] == ["tpm", "coder", "qa"]
    assert st["agents"][1]["model"]["model"] == "flash"
    assert st["agents"][0]["presence"]["state"] == "offline"
    assert "general" in st["channels"] and st["author"] == "matt"


def test_state_says_how_busy_each_agent_is(server):
    app, port, td, _ = server
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60))
    (td / "events.jsonl").write_text(json.dumps(
        {"ts": ts, "event": "turn", "agent": "coder", "duration_s": 900}) + "\n")
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    load = {a["name"]: a["load"] for a in st["agents"]}
    assert load["coder"]["busy"]["hour"] == 0.25 and load["coder"]["turns"] == 1
    assert load["qa"]["busy"] == {"hour": 0.0, "day": 0.0}


def test_posting_wakes_only_whom_it_mentions(server):
    app, port, td, _ = server
    _, r = call(port, "POST", "/api/post", {"team": "demo", "channel": "general",
                                            "content": "@coder look at allocate"}, token=app.token)
    assert r["woke"] == ["coder"]
    inboxes = {n: [json.loads(l) for l in (td / "inbox" / f"{n}.jsonl").read_text().splitlines()]
               for n in ("coder", "qa")}
    assert not inboxes["coder"][0].get("quiet") and inboxes["qa"][0]["quiet"]
    _, msgs = call(port, "GET", "/api/messages?team=demo&channel=general", token=app.token)
    [m] = msgs["messages"]
    assert m["author"] == "matt" and m["from"] == "user"


def test_threads_and_dms(server):
    app, port, td, _ = server
    _, root = call(port, "POST", "/api/post", {"team": "demo", "channel": "general",
                                               "content": "question for @qa"}, token=app.token)
    FileTransport("qa", {"team_dir": str(td)}).post("general", ["tpm", "coder"], "answer",
                                                    thread=root["id"])
    _, th = call(port, "GET", f"/api/messages?team=demo&thread={root['id']}", token=app.token)
    assert [m["content"] for m in th["messages"]] == ["question for @qa", "answer"]
    call(port, "POST", "/api/post", {"team": "demo", "to": "coder", "content": "hello"}, token=app.token)
    _, dm = call(port, "GET", "/api/messages?team=demo&dm=user,coder", token=app.token)
    assert [m["content"] for m in dm["messages"]] == ["hello"]


def test_channels_can_be_made_and_changed(server):
    app, port, *_ = server
    _, r = call(port, "POST", "/api/channel", {"team": "demo", "action": "create", "name": "billing",
                                               "members": ["coder", "qa"]}, token=app.token)
    assert r["members"] == ["coder", "qa"]
    status, err = call(port, "POST", "/api/channel", {"team": "demo", "action": "create",
                                                      "name": "billing"}, token=app.token)
    assert status == 400 and "already exists" in err["error"]


def test_a_channel_can_wake_a_member_on_every_post(server):
    app, port, *_ = server
    call(port, "POST", "/api/channel", {"team": "demo", "action": "create", "name": "docs",
                                        "members": ["coder", "qa"]}, token=app.token)
    _, r = call(port, "POST", "/api/channel", {"team": "demo", "action": "wake", "name": "docs",
                                               "agents": ["qa"]}, token=app.token)
    assert r["wake"] == ["qa"]
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert st["channels"]["docs"]["wake"] == ["qa"]
    _, r = call(port, "POST", "/api/post", {"team": "demo", "channel": "docs",
                                            "content": "please write up allocate"}, token=app.token)
    assert r["woke"] == ["qa"]
    status, err = call(port, "POST", "/api/channel", {"team": "demo", "action": "wake", "name": "docs",
                                                      "agents": ["tpm"]}, token=app.token)
    assert status == 400 and "not in #docs" in err["error"]


def test_the_model_can_be_changed_and_cleared(server):
    app, port, *_ = server
    call(port, "POST", "/api/model", {"team": "demo", "agent": "coder", "model": "opus",
                                      "until": "1d"}, token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    coder = st["agents"][1]["model"]
    assert coder["model"] == "opus" and coder["source"].startswith("override")
    call(port, "POST", "/api/model", {"team": "demo", "agent": "coder", "model": None}, token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert st["agents"][1]["model"]["model"] == "flash"


def test_artifacts_are_read_only_and_stay_inside_the_team(server):
    app, port, _, ws = server
    _, a = call(port, "GET", f"/api/artifact?team=demo&path={ws / 'report.md'}", token=app.token)
    assert a["kind"] == "markdown" and "allocate drops a cent" in a["content"]
    _, a = call(port, "GET", "/api/artifact?team=demo&path=/etc/passwd", token=app.token)
    assert "outside" in a["error"]
    _, a = call(port, "GET", f"/api/artifact?team=demo&path={ws}/../../../../etc/passwd", token=app.token)
    assert "outside" in a["error"]


def test_the_live_feed_carries_new_messages(server):
    app, port, td, _ = server
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/api/events?team=demo", headers={"Host": f"127.0.0.1:{port}", "X-Token": app.token})
    r = c.getresponse()
    assert r.getheader("Content-Type") == "text/event-stream"
    time.sleep(0.3)
    FileTransport("coder", {"team_dir": str(td)}).send("user", "done, see report.md")
    deadline = time.time() + 6
    got = b""
    while time.time() < deadline and b"done, see report.md" not in got:
        got += r.fp.read1(4096)
    assert b"done, see report.md" in got


def test_search(server):
    app, port, *_ = server
    call(port, "POST", "/api/post", {"team": "demo", "channel": "general",
                                     "content": "the kingfisher invoice"}, token=app.token)
    _, r = call(port, "GET", "/api/search?team=demo&q=kingfisher", token=app.token)
    assert r["hits"] and r["hits"][0]["type"] == "message"


def test_wiki(server):
    app, port, td, _ = server
    (td / "NORMS.md").write_text("## Review\nAlways run the proof.\n")
    mem = td.parent / "agents" / "qa" / "memory"
    mem.mkdir(parents=True)
    (mem / "proofs.md").write_text("run proofs before approving")
    _, w = call(port, "GET", "/api/wiki?team=demo", token=app.token)
    assert "Always run the proof" in w["norms"]
    assert w["memories"]["qa"][0]["name"] == "proofs"


def test_activity_shows_one_agents_events(server):
    app, port, td, _ = server
    with (td / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": "2026-10-18 10:00:00", "event": "tool_call", "agent": "coder",
                            "tool": "run_command", "args": {"CommandLine": "pytest"}}) + "\n")
        f.write(json.dumps({"ts": "2026-10-18 10:00:05", "event": "turn", "agent": "qa"}) + "\n")
    _, r = call(port, "GET", "/api/activity?team=demo&agent=coder", token=app.token)
    assert [e["tool"] for e in r["events"]] == ["run_command"]


def test_the_model_list_comes_from_the_runner(server, monkeypatch):
    from ateam import runner as runner_lib
    monkeypatch.setattr(runner_lib, "list_models",
                        lambda *a, **k: [{"id": "fast-high", "label": "Fast (High)"}])
    app, port, *_ = server
    _, r = call(port, "GET", "/api/models?team=demo", token=app.token)
    assert r["models"] == [{"id": "fast-high", "label": "Fast (High)"}]
    status, err = call(port, "POST", "/api/model", {"team": "demo", "agent": "coder",
                                                    "model": "not-offered"}, token=app.token)
    assert status == 400 and "fast-high" in err["error"]


def test_start_runs_the_team_on_its_rosters_runner_not_the_apps(server, monkeypatch):
    """Started from the app, a team got the app's environment: the default
    runner (its wakes failed, its conversations were lost) and a guessed
    manager."""
    import os
    import signal
    import sys
    app, port, td, _ = server
    doc = json.loads((td / "roster.json").read_text())
    doc["supervisor"] = {"runner": "team_runner:TeamRunner", "manager": "qa"}
    (td / "roster.json").write_text(json.dumps(doc))
    support = str(Path(__file__).parent / "support")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [support, os.environ.get("PYTHONPATH")])))
    monkeypatch.setenv("ATEAM_PYTHON", sys.executable)
    monkeypatch.setenv("ATEAM_RUNNER", "ateam.runner_agy:AgyRunner")    # the app's own
    status, r = call(port, "POST", "/api/supervisor?team=demo", {"action": "start"}, token=app.token)
    assert status == 200 and r["started"], r
    try:
        beat = None
        for _ in range(100):
            _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
            beat = st.get("supervisor")
            if beat:
                break
            time.sleep(0.1)
        assert beat, (td.parent / "supervisor.log").read_text() if (td.parent / "supervisor.log").exists() else "no heartbeat"
        assert beat["runner"] == "team-runner" and beat["source"]["runner"] == "roster"
        assert beat["manager"] == "qa"
    finally:
        try:
            os.killpg(r["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass



def test_the_model_list_is_the_teams_runners_not_the_apps(server, monkeypatch):
    """/api/models asked the app's environment, which names no runner: from a
    client started without the launcher's exports, the picker was empty."""
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "support"))
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "1")
    monkeypatch.delenv("ATEAM_RUNNER", raising=False)
    monkeypatch.delenv("ATEAM_RUNNER_CONFIG", raising=False)
    app, port, td, _ = server
    doc = json.loads((td / "roster.json").read_text())
    doc["supervisor"] = {"runner": "team_runner:ListingRunner",
                         "runner_config": {"offers": ["flash", "pro"]}}
    (td / "roster.json").write_text(json.dumps(doc))
    _, r = call(port, "GET", "/api/models?team=demo", token=app.token)
    assert r["runner"] == "listing-runner"
    assert [m["id"] for m in r["models"]] == ["flash", "pro"]
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    coder = next(a for a in st["agents"] if a["name"] == "coder")
    assert coder["model"]["available"] is True, coder


def test_bound_beyond_loopback_it_answers_to_ip_literals_not_other_names(team):
    root, td, _ = team
    app = App(root, [], "matt")
    httpd = serve(app, "127.0.0.1", 0)
    app.host = "0.0.0.0"            # as `--host 0.0.0.0` leaves it
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        assert call(port, "GET", "/api/teams", token=app.token, host="192.168.1.20")[0] == 200
        assert call(port, "GET", "/api/teams", token=app.token, host="evil.example")[0] == 403
        app.host = "chatbox.lan"    # `--host chatbox.lan`
        assert call(port, "GET", "/api/teams", token=app.token, host="chatbox.lan")[0] == 200
        app.host = "127.0.0.1"
        assert call(port, "GET", "/api/teams", token=app.token, host="192.168.1.20")[0] == 403
    finally:
        httpd.shutdown()


def test_the_command_line_takes_host_and_no_open(team):
    import subprocess
    import sys
    root, *_ = team
    p = subprocess.Popen([sys.executable, "-m", "ateam.chat", "--teams-root", str(root),
                          "--host", "127.0.0.1", "--port", "0", "--no-open"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        p.stdout.readline()
        url = p.stdout.readline().split()[-1]
        assert url.startswith("http://127.0.0.1:") and ":0/" not in url, url
        port = int(url.split(":")[2].split("/")[0])
        assert call(port, "GET", "/api/teams", token=url.split("token=")[1])[0] == 200
    finally:
        p.kill()
        p.wait()


# --- channels: archive, delete, restore ------------------------------------------

def test_a_channel_can_be_archived_deleted_and_restored(server):
    app, port, td, _ = server
    call(port, "POST", "/api/channel", {"team": "demo", "action": "create", "name": "old", "members": ["coder"]}, token=app.token)
    call(port, "POST", "/api/post", {"team": "demo", "channel": "old", "content": "keep this"}, token=app.token)
    status, _ = call(port, "POST", "/api/channel", {"team": "demo", "action": "archive", "name": "old"}, token=app.token)
    assert status == 200
    status, err = call(port, "POST", "/api/post", {"team": "demo", "channel": "old", "content": "x"}, token=app.token)
    assert status == 400 and "archived" in err["error"]
    _, r = call(port, "GET", "/api/messages?team=demo&channel=old", token=app.token)
    assert [m["content"] for m in r["messages"]] == ["keep this"], "archived is read-only, not hidden"
    call(port, "POST", "/api/channel", {"team": "demo", "action": "delete", "name": "old"}, token=app.token)
    _, r = call(port, "GET", "/api/messages?team=demo&channel=old", token=app.token)
    assert r["messages"] == []
    assert "keep this" in (td / "bus.jsonl").read_text(), "the record keeps it"
    call(port, "POST", "/api/channel", {"team": "demo", "action": "restore", "name": "old"}, token=app.token)
    _, r = call(port, "GET", "/api/messages?team=demo&channel=old", token=app.token)
    assert [m["content"] for m in r["messages"]] == ["keep this"]


# --- tasks and the board -----------------------------------------------------------

def test_a_task_made_here_is_linked_moved_and_explained(server):
    app, port, td, _ = server
    call(port, "POST", "/api/channel", {"team": "demo", "action": "create", "name": "billing", "members": ["coder", "qa"]}, token=app.token)
    status, t = call(port, "POST", "/api/task", {"team": "demo", "action": "create", "project": "billing",
                                                 "title": "fix allocate", "owner": "coder", "channel": "billing"}, token=app.token)
    assert status == 200 and t["channel"] == "billing", t
    status, t2 = call(port, "POST", "/api/task", {"team": "demo", "action": "update", "id": t["id"],
                                                  "status": "icebox", "rank": 3}, token=app.token)
    assert status == 200 and t2["status"] == "icebox" and t2["rank"] == 3
    told = [json.loads(x) for x in (td / "inbox" / "coder.jsonl").read_text().splitlines()]
    assert any("moved your task" in m["content"] for m in told)
    _, d = call(port, "GET", f"/api/task?team=demo&id={t['id']}", token=app.token)
    assert [e["kind"] for e in d["history"]] == ["created", "updated", "updated"]
    assert set(d["history"][1]) - {"id", "kind", "ts", "t", "by"} == {"thread"}, \
        "its first notice in #billing starts its thread"
    assert any(m.get("channel") == "billing" for m in d["messages"]), "the channel's notice is tagged with the task"
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert next(x for x in st["tasks"] if x["id"] == t["id"])["channel"] == "billing"
    _, all_ = call(port, "GET", "/api/tasks?team=demo", token=app.token)
    assert any(x["id"] == t["id"] for x in all_["tasks"])


def test_the_board_page_is_served_like_the_chat(server):
    app, port, *_ = server
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", f"/board?token={app.token}", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    assert r.status == 200 and b"board.js" in r.read()
    assert call(port, "GET", "/board")[0] == 401


# --- what people are called --------------------------------------------------------

def test_state_carries_aliases_the_operator_and_mentions_of_them(server):
    app, port, td, _ = server
    doc = json.loads((td / "roster.json").read_text())
    doc["operator"] = {"name": "Matt", "handles": ["roellem"]}
    doc["agents"][1]["alias"] = "Ada"
    (td / "roster.json").write_text(json.dumps(doc))
    from ateam import channels
    channels.create(td, "billing", ["coder"])
    channels.post(FileTransport("coder"), td, "billing", "@Matt which rounding?")
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert st["operator"]["name"] == "Matt" and "roellem" in st["operator"]["handles"]
    assert next(a for a in st["agents"] if a["name"] == "coder")["alias"] == "Ada"
    assert "c:billing" in st["mentions_of_user"]


def test_state_says_when_each_view_last_had_news_so_unread_survives_a_reload(server):
    app, port, td, _ = server
    from ateam import actions, channels
    channels.create(td, "billing", ["coder"])
    root = actions.post(td, "billing", "which rounding?", author="matt")
    channels.post(FileTransport("coder"), td, "billing", "banker's", thread=root["id"])
    FileTransport("coder").send("user", "done with the parser")
    actions.dm(td, "coder", "thanks")
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert set(st["latest"]) == {"c:billing", "d:coder,user"}
    # A direct message to you is addressed to you; your own messages are not news.
    assert "d:coder,user" in st["mentions_of_user"] and "c:billing" not in st["mentions_of_user"]


def test_admin_says_what_channel_mail_costs(server):
    app, port, td, _ = server
    (td / "events.jsonl").write_text(json.dumps(
        {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "turn", "agent": "coder",
         "input_tokens": 4000, "trigger": {"quiet": {
             "msgs": 3, "bytes": 400, "deferred": 0, "channels": {"docs": 400}}}}) + "\n")
    _, doc = call(port, "GET", "/api/admin?team=demo", token=app.token)
    q = doc["quiet_mail"]
    assert q["msgs"] == 3 and q["est_tokens"] == 100 and q["share"] == 0.025
    assert q["channels"] == {"docs": 400}
