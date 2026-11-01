"""Timestamps with instants, task usage, files and pictures, attachments,
bookmarks, and artifact paths -- over HTTP."""
import base64
import http.client
import json
import time
from pathlib import Path

from agyteam.transport_file import FileTransport
from test_api import call, server, team  # noqa: F401  (fixtures)


def raw(port, path, token):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "X-Token": token})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_messages_carry_the_instant_they_were_written(server):
    app, port, *_ = server
    call(port, "POST", "/api/post", {"team": "demo", "channel": "general", "content": "hi"},
         token=app.token)
    _, msgs = call(port, "GET", "/api/messages?team=demo&channel=general", token=app.token)
    [m] = msgs["messages"]
    assert abs(m["t"] - time.time()) < 120


def test_tasks_carry_their_tokens(server):
    app, port, td, _ = server
    _, t = call(port, "POST", "/api/task", {"team": "demo", "action": "create", "project": "p",
                                            "title": "named work"}, token=app.token)
    tid = t["id"]
    with (td / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "turn", "agent": "coder",
                            "input_tokens": 1000, "output_tokens": 200,
                            "trigger": {"task_ids": [tid]}}) + "\n")
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    row = next(x for x in st["tasks"] if x["id"] == tid)
    assert row["usage"]["tokens"] == 1200 and row["t_created"]


def test_an_attachment_is_saved_and_served_sandboxed(server):
    app, port, td, _ = server
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 2_000_000                 # bigger than the 1 MB JSON cap
    status, r = call(port, "POST", "/api/attach", {"team": "demo", "name": "shot.png",
                                                   "data": base64.b64encode(png).decode()},
                     token=app.token)
    assert status == 200 and Path(r["path"]).read_bytes() == png
    status, headers, body = raw(port, f"/api/file?team=demo&path={r['path']}", app.token)
    assert status == 200 and body == png and headers["Content-Type"] == "image/png"
    assert "sandbox" in headers["Content-Security-Policy"]


def test_pictures_for_agents(server):
    app, port, td, _ = server
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    call(port, "POST", "/api/avatar", {"team": "demo", "who": "coder", "ext": ".svg",
                                       "data": base64.b64encode(svg).decode()}, token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert st["avatars"] == {"coder": "coder.svg"}
    status, headers, body = raw(port, "/api/avatar?team=demo&who=coder", app.token)
    assert body == svg and "script-src 'none'" in headers["Content-Security-Policy"]


def test_bookmarks_on_channels_and_tasks(server):
    app, port, *_ = server
    call(port, "POST", "/api/bookmarks", {"team": "demo", "channel": "general",
                                          "bookmarks": [{"title": "Runbook", "url": "go/runbook"}]},
         token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert st["channels"]["general"]["bookmarks"] == [{"title": "Runbook", "url": "go/runbook"}]
    _, t = call(port, "POST", "/api/task", {"team": "demo", "action": "create", "project": "p",
                                            "title": "x"}, token=app.token)
    call(port, "POST", "/api/bookmarks", {"team": "demo", "task": t["id"],
                                          "bookmarks": [{"url": "https://example.com/pr/1"}]},
         token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert next(x for x in st["tasks"] if x["id"] == t["id"])["links"][0]["url"] == "https://example.com/pr/1"


def test_relative_paths_open_and_hidden_home_files_do_not(server, monkeypatch, tmp_path):
    app, port, td, ws = server
    (td.parent / "docs").mkdir()
    (td.parent / "docs" / "plan.md").write_text("# Plan\n")
    _, a = call(port, "GET", "/api/artifact?team=demo&path=docs/plan.md", token=app.token)
    assert a["kind"] == "markdown" and a["content"] == "# Plan\n"
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id").write_text("secret")
    (home / "notes.md").write_text("mine")
    monkeypatch.setenv("HOME", str(home))
    _, a = call(port, "GET", "/api/artifact?team=demo&path=~/notes.md", token=app.token)
    assert a.get("content") == "mine"
    _, a = call(port, "GET", "/api/artifact?team=demo&path=~/.ssh/id", token=app.token)
    assert "content" not in a and "outside" in a["error"]


def test_state_says_which_version_is_serving(server):
    """An open tab keeps the code it loaded; the page compares this with
    what it loaded and offers a reload."""
    import agychat
    app, port, *_ = server
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert st["app_version"] == agychat.__version__



def test_state_lists_what_is_waiting_on_a_person(server):
    app, _, td, _ = server
    FileTransport("coder", {"team_dir": str(td)}).send("user", "Which database?", kind="question")
    row = app.state("demo")["needs_you"][0]
    assert row["kind"] == "question" and row["from"] == "coder" and row["dm"] == "coder"


def test_nothing_waiting_is_an_empty_list(server):
    assert server[0].state("demo")["needs_you"] == []


def test_the_board_sets_what_a_task_waits_for_and_writes(server):
    app, *_ = server
    first = app.task_action("demo", {"action": "create", "title": "first"})
    second = app.task_action("demo", {"action": "create", "title": "second"})
    done = app.task_action("demo", {"action": "update", "id": second["id"],
                                    "blocked_by": [first["id"]], "writes": ["src/"]})
    assert done["blocked_by"] == [first["id"]] and done["writes"] == ["src"]
    row = next(t for t in app.tasks("demo") if t["id"] == second["id"])
    assert row["blocked_by"] == [first["id"]] and row["writes"] == ["src"]
