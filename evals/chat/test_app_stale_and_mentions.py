"""Two things a person saw and reported:

"I'm still not getting red @ notifications for channels - just DMs." Agents
see the operator's name on what they post and address them by it; @thatname
was a mention only when the roster had an operator block naming it, and
what counted was whatever the agent's code recorded at the time. These pin
that the app reads the @ itself, for @user, the operator's handles and the
name the operator posts as.

"Deleting a message doesn't work - I get a red box with 'unknown endpoint'."
The page is read from disk on every load; the server runs the code it
started with. An upgrade under a running server gave a page whose delete
the old server did not know. These pin that the server says when the
installed code is newer than itself, that it can restart onto it, and that
the page says so rather than showing the bare error.
"""
import json
import threading
from pathlib import Path

import pytest

from ateam import channels
from ateam.chat import server as server_lib
from ateam.chat.server import App, serve
from ateam.transport_file import FileTransport

from test_app_api import call

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def server(tmp_path, monkeypatch):
    root = tmp_path / "teams"
    td = root / "demo" / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "implements"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.delenv("ATEAM_BUS_TRANSPORT", raising=False)
    app = App(root, [], "roelle")
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    channels.create(td, "billing", ["coder", "qa"])
    channels.create(td, "release", ["qa"])
    yield app, httpd.server_address[1], td
    httpd.shutdown()


def state(app, port):
    return call(port, "GET", "/api/state?team=demo", token=app.token)[1]


def test_an_at_of_the_name_you_post_as_is_a_mention_with_no_operator_block(server):
    app, port, td = server
    channels.post(FileTransport("qa"), td, "release", "@roelle ok to ship?")
    channels.post(FileTransport("coder"), td, "billing", "rounding is fixed")
    st = state(app, port)
    assert "roelle" in st["my_handles"] and "user" in st["my_handles"]
    assert "c:release" in st["mentions_of_user"] and "c:billing" not in st["mentions_of_user"]


def test_a_mention_counts_whatever_the_posting_code_recorded(server):
    app, port, td = server
    with (td / "bus.jsonl").open("a") as f:     # as an agent on older code wrote it
        f.write(json.dumps({"id": "a1", "ts": "2026-11-06 09:00:00", "from": "coder",
                            "to": "#billing", "channel": "billing",
                            "content": "@user which rounding?"}) + "\n")
    assert "c:billing" in state(app, port)["mentions_of_user"]


def test_the_operators_handles_still_count_and_an_email_is_not_a_mention(server):
    app, port, td = server
    doc = json.loads((td / "roster.json").read_text())
    doc["operator"] = {"name": "Matt", "handles": ["roellem"]}
    (td / "roster.json").write_text(json.dumps(doc))
    channels.post(FileTransport("coder"), td, "billing", "mail matt@example.com about it")
    assert "c:billing" not in state(app, port)["mentions_of_user"]
    channels.post(FileTransport("qa"), td, "release", "@ROELLEM ok to ship?")
    st = state(app, port)
    assert {"matt", "roellem", "roelle", "user"} <= set(st["my_handles"])
    assert "c:release" in st["mentions_of_user"]


def test_the_server_says_when_the_installed_code_is_newer(server, monkeypatch):
    app, port, td = server
    st = state(app, port)
    assert st["installed_version"] == st["app_version"]
    monkeypatch.setattr(server_lib, "_installed_version", lambda: "99.0.0")
    assert state(app, port)["installed_version"] == "99.0.0"


def test_restart_reexecs_onto_the_installed_code(server):
    app, port, td = server
    done = threading.Event()
    app.reexec = done.set
    status, out = call(port, "POST", "/api/update", {"team": "demo", "action": "restart"},
                       token=app.token)
    assert status == 200 and out == {"restarting": True}
    assert done.wait(5)


def test_the_installed_version_is_read_from_disk():
    import ateam
    assert server_lib._installed_version() == ateam.__version__


def test_the_page_reads_the_at_and_names_a_stale_server():
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    assert "S.state?.my_handles" in app and "HANDLE_RE" in app
    assert 'j.error === "unknown endpoint"' in app and "function staleServer" in app
    assert "installed_version" in app and 'action: "restart"' in app
