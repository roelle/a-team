"""What a person asked of the app, and a bug they saw:

"Need to be able to mark items as unread or 'needs followup' (add to a list
to get back to). Assigning a task to the main user might be a way." These
pin that a task the operator owns waits on them and tells nobody, and that
the page can mark unread and follow up from any message.

"It would be nice to be able to link to messages and threads and
cross-link inside the app. Right click 'link to message'." These pin that a
message can be looked up by its id alone, and that a link to one opens in
the app named for what it points at.

"Can we make multiple tasks from one message? From a message inside a
thread? Can the task link back to a specific message?" These pin that every
message, a reply included, offers a task, and that the board's link back
names the message.

"Messages sent in a thread end up in the main channel pane, and later move
back to being threaded." The live feed sent a reply to a reply as written,
naming the reply rather than the first message, and a reply could land in
the list of the view being left. These pin that the feed gives the same
thread a reload does, and that a view's first page brings the first message
of each thread it shows.
"""
import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from ateam import actions, channels
from ateam.chat.server import App, serve
from ateam.record import Record
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
    channels.create(td, "billing", ["coder", "qa"])
    app = App(root, [], "roelle")
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, httpd.server_address[1], td
    httpd.shutdown()


def get(app, port, path):
    return call(port, "GET", path, token=app.token)


def test_a_message_is_found_by_its_id_alone(server):
    app, port, td = server
    root = channels.post(FileTransport("coder"), td, "billing", "which rounding?")["id"]
    reply = channels.post(FileTransport("qa"), td, "billing", "half-even", thread=root)["id"]
    status, out = get(app, port, f"/api/messages?team=demo&id={reply}")
    assert status == 200 and out["message"]["thread"] == root
    assert out["message"]["channel"] == "billing" and out["message"]["t"]
    status, out = get(app, port, "/api/messages?team=demo&id=nope")
    assert status >= 400 and "no message" in out["error"]
    actions.delete_channel(td, "billing")
    status, out = get(app, port, f"/api/messages?team=demo&id={root}")
    assert status >= 400 and "deleted" in out["error"]


def test_a_view_brings_the_first_message_of_each_thread_it_shows(server):
    app, port, td = server
    root = channels.post(FileTransport("coder"), td, "billing", "the old question")["id"]
    for i in range(5):
        channels.post(FileTransport("coder"), td, "billing", f"chatter {i}")
    channels.post(FileTransport("qa"), td, "billing", "answering the old one", thread=root)
    _, out = get(app, port, "/api/messages?team=demo&channel=billing&limit=3")
    ids = [m["id"] for m in out["messages"]]
    assert ids[0] == root and len(ids) == 4, "the reply's first message comes along"


def read_events(port, token, until, timeout=8):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request("GET", "/api/events?team=demo", headers={"Host": f"127.0.0.1:{port}", "X-Token": token})
    r = c.getresponse()
    end = time.time() + timeout
    while time.time() < end:
        line = r.fp.readline().decode()
        if line.startswith("data: "):
            ev = json.loads(line[6:])
            if until(ev):
                c.close()
                return ev
    c.close()
    raise AssertionError("no such event")


def test_the_live_feed_puts_a_reply_to_a_reply_in_the_first_messages_thread(server):
    app, port, td = server
    root = channels.post(FileTransport("coder"), td, "billing", "which rounding?")["id"]
    first = channels.post(FileTransport("qa"), td, "billing", "half-even", thread=root)["id"]
    got = {}

    def watch():
        got["ev"] = read_events(port, app.token, lambda ev: ev.get("stream") == "bus"
                                and ev["item"].get("content") == "agreed")
    w = threading.Thread(target=watch)
    w.start()
    time.sleep(1.0)                    # the feed starts from the end of the log
    with (td / "bus.jsonl").open("a") as f:       # as an agent on older code wrote it
        f.write(json.dumps({"id": "r2", "ts": "2026-11-10 09:00:00", "from": "coder", "to": "#billing",
                            "channel": "billing", "content": "agreed", "thread": first}) + "\n")
    w.join(10)
    assert got["ev"]["item"]["thread"] == root
    assert Record(td).roots()[first] == root


def test_a_task_the_operator_owns_waits_on_them_and_tells_nobody(server):
    app, port, td = server
    m = channels.post(FileTransport("coder"), td, "billing", "can you check the invoice totals?")
    before = (td / "bus.jsonl").read_text()
    status, t = call(port, "POST", "/api/task", {"team": "demo", "action": "create", "owner": "user",
                                                 "title": "Follow up: invoice totals",
                                                 "origin": {"id": m["id"], "channel": "billing"}},
                     token=app.token)
    assert status == 200 and t["owner"] == "user"
    assert (td / "bus.jsonl").read_text() == before, "nobody is sent anything"
    _, st = get(app, port, "/api/state?team=demo")
    row = next(r for r in st["needs_you"] if r["kind"] == "task")
    assert row["id"] == t["id"] and row["origin"]["id"] == m["id"]
    call(port, "POST", "/api/task", {"team": "demo", "action": "update", "id": t["id"], "status": "done"},
         token=app.token)
    _, st = get(app, port, "/api/state?team=demo")
    assert not [r for r in st["needs_you"] if r["kind"] == "task"]
    assert (td / "bus.jsonl").read_text() == before


def test_one_message_can_make_several_tasks_that_each_link_back(server):
    app, port, td = server
    root = channels.post(FileTransport("coder"), td, "billing", "two things are wrong")["id"]
    reply = channels.post(FileTransport("qa"), td, "billing", "and a third", thread=root)["id"]
    for title, src in (("first", root), ("second", root), ("third", reply)):
        call(port, "POST", "/api/task", {"team": "demo", "action": "create", "title": title,
                                         "origin": {"id": src, "channel": "billing",
                                                    "thread": root if src == reply else None}},
             token=app.token)
    _, st = get(app, port, "/api/state?team=demo")
    by = {}
    for t in st["tasks"]:
        by.setdefault(t["origin"]["id"], []).append(t["title"])
    assert sorted(by[root]) == ["first", "second"] and by[reply] == ["third"]


def test_the_page_links_marks_unread_and_follows_up():
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    for needle in ('addEventListener("contextmenu"', "function msgMenu", "Copy link to message",
                   "Copy link to thread", "function markUnread", "function followUp",
                   "function goToMessage", "data-done-task", "function appLink",
                   "S.messagesFor === k", 'owner: "user"', 'data-mktask="${esc(m.id)}">make a task'):
        assert needle in app, needle
    # make a task is offered on every message: not only before a reply, and in threads
    acts = app[app.index("const acts = m.id"):app.index("const replies =")]
    assert "inThread" in acts.split("make a task")[0] and "mktask" in acts
    common = (ROOT / "ateam/chat/static/common.js").read_text()
    assert "ctx.appLink?.(" in common and "function appLinkHTML" in common
    board = (ROOT / "ateam/chat/static/board.js").read_text()
    assert "&msg=${encodeURIComponent(o.id)}" in board


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
