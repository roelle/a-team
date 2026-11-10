"""Three things a person reported about the app:

"The app is getting sluggish. 20MB in 11k messages. One channel takes .25s.
Every channel view re-reads the full message log." These pin that the
message log is parsed once and then only what was appended, that a log
rewritten or replaced is read again, and that what the app derives from it
is worked out again only when it changes.

"Channels should show unread (bold) if any threads are unread in the
channel - not clear just when the channel is opened." These pin that the
state says when each thread in a view last had a reply worth reading.

"When a code block is long, it sets the pane width for all text." These pin
that a message's column cannot grow past its pane, and that code wraps.
"""
import json
import threading
from pathlib import Path

import pytest

from ateam import actions, channels, record
from ateam.chat.server import App, serve
from ateam.record import Record
from ateam.transport_file import FileTransport

from test_app_api import call

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def team(tmp_path, monkeypatch):
    root = tmp_path / "teams"
    td = root / "demo" / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "implements"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.delenv("ATEAM_BUS_TRANSPORT", raising=False)
    channels.create(td, "billing", ["coder", "qa"])
    channels.create(td, "docs", ["coder"])
    return root, td


def row(i, **kw):
    return json.dumps({"id": f"m{i}", "ts": f"2026-11-09 10:{i:02d}:00", "from": "coder",
                       "to": "#billing", "channel": "billing", "content": f"n{i}", **kw}) + "\n"


def parses(monkeypatch):
    n = {"lines": 0}
    real = record.json.loads

    def counting(s, *a, **k):
        n["lines"] += 1
        return real(s, *a, **k)
    monkeypatch.setattr(record.json, "loads", counting)
    return n


def test_the_log_is_parsed_once_and_then_only_what_was_appended(team, monkeypatch):
    _, td = team
    (td / "bus.jsonl").write_text("".join(row(i) for i in range(20)))
    assert len(Record(td).bus()) == 20
    n = parses(monkeypatch)
    assert len(Record(td).bus()) == 20 and n["lines"] == 0
    with (td / "bus.jsonl").open("a") as f:
        f.write(row(20) + row(21))
    assert [r["id"] for r in Record(td).bus()][-2:] == ["m20", "m21"]
    assert n["lines"] == 2


def test_a_line_still_being_written_waits_for_its_end(team):
    _, td = team
    (td / "bus.jsonl").write_text(row(0) + row(1)[:20])
    assert [r["id"] for r in Record(td).bus()] == ["m0"]
    with (td / "bus.jsonl").open("a") as f:
        f.write(row(1)[20:])
    assert [r["id"] for r in Record(td).bus()] == ["m0", "m1"]


def test_a_log_rewritten_in_place_or_replaced_is_read_again(team):
    _, td = team
    bus = td / "bus.jsonl"
    bus.write_text(row(0) + row(1))
    Record(td).bus()
    bus.write_text(row(0, content="changed") + row(1) + row(2))     # same file, longer
    assert [r["content"] for r in Record(td).bus()] == ["changed", "n1", "n2"]
    tmp = td / "bus.new"
    tmp.write_text(row(5))
    tmp.replace(bus)                                                 # a new file
    assert [r["id"] for r in Record(td).bus()] == ["m5"]


def test_an_edit_shows_although_the_log_did_not_change(team):
    _, td = team
    sent = actions.post(td, "billing", "which rounding?", author="matt")
    assert Record(td).bus()[-1]["content"] == "which rounding?"
    actions.edit_message(td, sent["id"], "which rounding, half-even?", by="matt")
    assert Record(td).bus()[-1]["content"] == "which rounding, half-even?"


def test_what_is_derived_from_the_log_follows_it(team):
    _, td = team
    root = channels.post(FileTransport("coder"), td, "billing", "q")["id"]
    assert Record(td).reply_counts() == {}
    channels.post(FileTransport("qa"), td, "billing", "a", thread=root)
    assert Record(td).reply_counts() == {root: 1}
    FileTransport("coder").send("qa", "hi")
    assert ("coder", "qa") in Record(td).conversations()


@pytest.fixture
def server(team):
    root, td = team
    app = App(root, [], "roelle")
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, httpd.server_address[1], td
    httpd.shutdown()


def test_the_state_says_when_each_thread_last_had_a_reply_worth_reading(server):
    app, port, td = server
    root = actions.post(td, "billing", "which rounding?", author="roelle")["id"]
    other = channels.post(FileTransport("coder"), td, "billing", "and refunds?")["id"]
    channels.post(FileTransport("coder"), td, "billing", "half-even", thread=root)
    channels.post(FileTransport("qa"), td, "billing", "seen", thread=other, kind="status")
    actions.post(td, "billing", "thanks", author="roelle", thread=other)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    threads = st["thread_latest"]["c:billing"]
    assert set(threads) == {root}, "a quiet update or your own reply marks nothing"
    assert threads[root] == Record(td).bus()[2]["ts"]


def test_the_state_keeps_to_the_most_recent_threads(server, monkeypatch):
    app, port, td = server
    monkeypatch.setattr(App, "THREADS_PER_VIEW", 3)
    roots = [channels.post(FileTransport("coder"), td, "docs", f"q{i}")["id"] for i in range(5)]
    for r in roots:
        channels.post(FileTransport("coder"), td, "docs", "a", thread=r)
    channels.post(FileTransport("coder"), td, "docs", "again", thread=roots[0])
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    assert list(st["thread_latest"]["c:docs"]) == [roots[3], roots[4], roots[0]]


def test_the_page_marks_a_channel_with_an_unread_thread():
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    assert "function threadUnread" in app and "|| threadUnread(k)" in app
    assert "S.state.thread_latest" in app


def test_a_long_code_line_does_not_widen_the_pane():
    css = (ROOT / "ateam/chat/static/app.css").read_text()
    assert "grid-template-columns: 36px minmax(0, 1fr)" in css
    pre = next(l for l in css.splitlines() if l.startswith(".body pre {"))
    assert "white-space: pre-wrap" in pre
