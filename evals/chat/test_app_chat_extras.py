"""Timestamps with instants, task usage, files and pictures, attachments,
bookmarks, and artifact paths -- over HTTP."""
import base64
import http.client
import json
import subprocess
import time
from pathlib import Path

import pytest

from ateam.chat import server as server_module
from ateam.chat.server import App
from ateam.chat.teams import Team
from ateam import actions, install_plugin, starter
from ateam.transport_file import FileTransport
from test_app_api import call, server, team  # noqa: F401  (fixtures)


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
    import ateam.chat as agychat
    app, port, *_ = server
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert st["app_version"] == agychat.__version__



def test_state_lists_what_is_waiting_on_a_person(server):
    app, _, td, _ = server
    FileTransport("coder", {"team_dir": str(td)}).send("user", "Which database?", kind="question")
    row = app.state("demo")["needs_you"][0]
    assert row["kind"] == "question" and row["from"] == "coder" and row["dm"] == "coder"


def test_notices_clear_over_http(server):
    app, port, td, _ = server
    bus = FileTransport("supervisor", {"team_dir": str(td)})
    bus.send("user", "one", kind="blocker")
    bus.send("user", "two", kind="blocker")
    first = app.state("demo")["needs_you"][0]["id"]
    _, r = call(port, "POST", "/api/clear_waiting", {"team": "demo", "ids": [first]}, token=app.token)
    assert r["cleared"] == 1
    _, r = call(port, "POST", "/api/clear_waiting", {"team": "demo", "from": "supervisor"}, token=app.token)
    assert r["cleared"] == 1 and app.state("demo")["needs_you"] == []


def test_notices_from_before_ids_clear_over_http(server):
    """Notices written before messages had ids: the × and Clear all use each
    row's key, which every message has."""
    app, port, td, _ = server
    with (Path(td) / "bus.jsonl").open("a") as f:
        for n in range(2):
            f.write(json.dumps({"ts": f"2026-10-02 09:00:0{n}", "from": "supervisor", "to": "user",
                                "kind": "blocker", "content": f"waiting for you {n}"}) + "\n")
    first = app.state("demo")["needs_you"][0]
    assert first["key"] and not first["id"]
    _, r = call(port, "POST", "/api/clear_waiting", {"team": "demo", "ids": [first["key"]]}, token=app.token)
    assert r["cleared"] == 1
    _, r = call(port, "POST", "/api/clear_waiting", {"team": "demo", "from": "supervisor"}, token=app.token)
    assert r["cleared"] == 1 and app.state("demo")["needs_you"] == []


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


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """An app with no teams, on a machine where only the agy CLI is ready."""
    monkeypatch.delenv("ATEAM_TEAM_DIR", raising=False)
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.setattr(starter.shutil, "which", lambda name: "/usr/bin/agy")
    monkeypatch.setattr(starter, "AGY_SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(install_plugin, "install",
                        lambda: subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(Team, "start_supervisor", lambda self: {"started": True, "pid": 1})
    return App(tmp_path / "teams", [], "matt")


def test_a_first_run_says_what_the_machine_can_run(fresh):
    got = fresh.setup()
    assert got["teams_root"].endswith("teams")
    agy = next(r for r in got["runtimes"] if r["id"] == "agy")
    assert agy["ready"] and "toolPermission" in agy["note"]


def test_a_first_run_makes_a_team_that_shows_up_and_starts(fresh):
    made = fresh.new_team({"name": "default", "runtime": "agy"})
    assert made["team"] == "default" and made["supervisor"]["started"]
    assert "toolPermission" in made["note"]
    assert [t["name"] for t in fresh.teams()] == ["default"]
    assert [a["name"] for a in fresh.state("default")["agents"]][0] == "manager"


def test_a_team_that_cannot_start_is_still_made_and_says_why(fresh, monkeypatch):
    def fails(self):
        raise OSError("no such interpreter")
    monkeypatch.setattr(Team, "start_supervisor", fails)
    made = fresh.new_team({"name": "t", "runtime": "agy"})
    assert made["supervisor"] == {"started": False, "reason": "no such interpreter"}


def test_a_runtime_that_is_not_ready_is_refused(fresh):
    with pytest.raises(actions.OperatorError, match="not ready"):
        fresh.new_team({"name": "t", "runtime": "claude"})


def test_without_a_teams_root_there_is_nowhere_to_put_one(fresh):
    fresh.teams_root = None
    with pytest.raises(actions.OperatorError, match="--team-dir"):
        fresh.new_team({"name": "t", "runtime": "agy"})


@pytest.mark.parametrize("asked,host,tty,ssh,opens", [
    (None, "127.0.0.1", True, None, True),
    (None, "127.0.0.1", False, None, False),            # a service
    (None, "127.0.0.1", True, "1.2.3.4 5 6 7", False),
    (None, "0.0.0.0", True, None, False),
    (True, "0.0.0.0", False, None, True),
    (False, "127.0.0.1", True, None, False)])
def test_who_gets_a_browser(monkeypatch, asked, host, tty, ssh, opens):
    monkeypatch.setattr(server_module.sys.stdout, "isatty", lambda: tty, raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    if ssh:
        monkeypatch.setenv("SSH_CONNECTION", ssh)
    assert server_module.opens_browser(asked, host) is opens


def test_a_restart_opens_no_second_tab(tmp_path):
    app = App(tmp_path, [], "matt")
    app.argv = ["--open", "--port", "9000"]
    args = app.restart_args()
    assert args == ["--port", "9000", "--no-open"]
    parsed = server_module.opens_browser(False if "--no-open" in args else None, "127.0.0.1")
    assert parsed is False


def test_a_restart_brings_back_the_teams_not_stopped_on_purpose(fresh, monkeypatch):
    fresh.new_team({"name": "running", "runtime": "agy"})
    fresh.new_team({"name": "parked", "runtime": "agy"})
    actions.stop(fresh.team("parked").dir, "on hold")
    started = []
    monkeypatch.setattr(Team, "start_supervisor",
                        lambda self: started.append(self.name) or {"started": True})
    said = server_module.start_teams(fresh)
    assert started == ["running"]
    assert "parked stays stopped: on hold" in said and "running started" in said


def test_a_team_that_cannot_start_does_not_stop_the_others(fresh, monkeypatch):
    fresh.new_team({"name": "a", "runtime": "agy"})

    def fails(self):
        raise OSError("no python")
    monkeypatch.setattr(Team, "start_supervisor", fails)
    assert server_module.start_teams(fresh) == ["a did not start: no python"]


def test_sigterm_ends_the_server_promptly(tmp_path):
    import signal
    import sys
    p = subprocess.Popen([sys.executable, "-m", "ateam.chat", "--teams-root", str(tmp_path),
                          "--port", "0", "--no-open"], stdout=subprocess.PIPE, text=True)
    p.stdout.readline()
    p.stdout.readline()
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=5) == 0


def test_a_tasks_detail_names_what_it_overlaps(server):
    app, *_ = server
    a = app.task_action("demo", {"action": "create", "title": "api"})
    b = app.task_action("demo", {"action": "create", "title": "docs"})
    app.task_action("demo", {"action": "update", "id": a["id"], "writes": ["src/api"]})
    app.task_action("demo", {"action": "update", "id": b["id"], "writes": ["src/api/README.md"]})
    assert [o["title"] for o in app.task("demo", b["id"])["overlaps"]] == ["api"]


def test_start_runs_the_rosters_launch_command(tmp_path, monkeypatch):
    """A host whose supervisor needs its own interpreter or wrapper says how
    to start it, and the Start button runs that instead of guessing."""
    import sys
    from ateam import actions
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    td = tmp_path / "home" / "team"
    td.mkdir(parents=True)
    script = ("import os, sys; open(sys.argv[1], 'w').write("
              "' '.join(sys.argv[2:]) + ' ' + os.environ['ATEAM_TEAM_DIR'])")
    (td / "roster.json").write_text(json.dumps({"agents": [], "supervisor": {
        "launch": [sys.executable, "-c", script, "{team_dir}/launched.txt", "{team}", "--daemon"]}}))
    team = Team(td)
    got = team.start_supervisor()
    assert got["started"]
    out = td / "launched.txt"
    for _ in range(100):
        if out.exists() and out.read_text():
            break
        time.sleep(0.05)
    assert out.read_text() == f"home --daemon {td.resolve()}"
    (td / "roster.json").write_text(json.dumps({"agents": [], "supervisor": {"launch": "run-it"}}))
    with pytest.raises(actions.OperatorError, match="list of strings"):
        team.start_supervisor()
