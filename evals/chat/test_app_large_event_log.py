"""The app on a team that has been working for a while.

"Activity and Board are really slow, like 10-60s. I'm not even sure
activity works. Even just opening board hangs the app for minutes."

The event log is mostly tool calls, each carrying what the tool returned;
a busy team's runs to hundreds of megabytes. One refresh read and parsed
all of it three times, the board's did so five times, every tool call the
team made told every open page to refresh, and refreshes that overlapped
each slowed the others. At 200 MB a refresh took 3.8 seconds and six
together 14; after, 16 milliseconds and a tenth of a second.

These pin that:
- the tool calls are never parsed to draw a page, and nothing is parsed
  twice;
- a tool call does not send the pages to look again, nor have the board's
  token counts worked out again;
- an agent's Activity is read from the end of the log, however long it is;
- people asking for the state together share one answer, never an older
  one than they asked for;
- the supervisor's reads of the log cost what was appended, not all of it.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ateam import activity, channels, logtail, record
from ateam.chat.server import App, Shared, serve
from ateam.observer_file import FileObserver
from ateam.record import Feed, Record

from test_app_api import call

ROOT = Path(__file__).resolve().parents[2]


def ev(i, kind="tool_call", agent="coder", **kw):
    row = {"ts": f"2026-11-12 10:{i // 60 % 60:02d}:{i % 60:02d}", "event": kind, "agent": agent}
    if kind == "tool_call":
        row.update(tool="Bash", args={"command": f"pytest -k t{i}"}, result="ok " * 50, error=None)
    if kind == "turn":
        row.update(duration_s=30.0, input_tokens=1000, output_tokens=100, model="m")
    return json.dumps({**row, **kw}) + "\n"


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
    lines = []
    for i in range(300):
        lines.append(ev(i, agent="coder" if i % 3 else "qa"))
        if i % 50 == 49:
            lines.append(ev(i, "turn", agent="coder"))
    (td / "events.jsonl").write_text("".join(lines))
    return root, td


@pytest.fixture
def server(team):
    root, td = team
    app = App(root, [], "roelle")
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, httpd.server_address[1], td
    httpd.shutdown()


def parsed(monkeypatch):
    """Every line parsed from here on."""
    seen = []
    real = json.loads

    def counting(s, *a, **k):
        seen.append(s if isinstance(s, (bytes, str)) else b"")
        return real(s, *a, **k)
    monkeypatch.setattr(json, "loads", counting)
    return seen


def calls_in(seen):
    return [s for s in seen if b"tool_call" in (s if isinstance(s, bytes) else s.encode())]


# --- drawing a page ------------------------------------------------------------------

def test_a_page_is_drawn_without_parsing_a_tool_call(team, monkeypatch):
    root, td = team
    app = App(root, [], "roelle")
    seen = parsed(monkeypatch)
    state = app.state("demo")
    app.tasks("demo")
    assert not calls_in(seen), "tool calls were parsed to draw the page"
    assert {a["name"]: a["load"]["turns"] for a in state["agents"]} == {"coder": 6, "qa": 0}


def test_a_second_look_parses_none_of_the_log_again(team, monkeypatch):
    root, td = team
    app = App(root, [], "roelle")
    app.state("demo"), app.tasks("demo")
    seen = parsed(monkeypatch)
    app.state("demo"), app.tasks("demo")
    assert not [s for s in seen if b'"event"' in (s if isinstance(s, bytes) else s.encode())]


def test_only_what_was_appended_is_read(team, monkeypatch):
    _, td = team
    assert len(Record(td).events("turn")) == 6
    seen = parsed(monkeypatch)
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(400) + ev(401, "turn", agent="qa") + ev(402))
    assert len(Record(td).events("turn")) == 7
    assert len(seen) == 1, "more than the one new turn was parsed"


def test_what_the_page_says_is_what_the_whole_log_says(team):
    """Left out of memory, not out of the answers: who is failing, how busy
    each is, and when each last worked, are as a full read gives them."""
    _, td = team
    with (td / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "failure",
                            "agent": "qa", "error": "boom", "duration_s": 5}) + "\n")
        f.write(ev(500, agent="qa"))
    rec = Record(td)
    (td / "supervisor.json").write_text("{}")
    last = {}
    for e in rec.events():                          # the whole log, tool calls and all
        if e.get("event") in ("turn", "failure"):
            last[e["agent"]] = e
    assert rec._last_turns() == last
    assert rec.load()["qa"]["turns"] == 1 and rec.load()["coder"]["turns"] == 6
    assert rec.load()["coder"]["busy_s"] == 180


def test_a_tool_call_written_another_way_is_still_left_out(team):
    """The saving is in not parsing lines that say early on they are tool
    calls. One that says so elsewhere, or without the space, is parsed and
    left out all the same."""
    _, td = team
    with (td / "events.jsonl").open("a") as f:
        f.write('{"agent":"coder","result":"' + "x" * 200 + '","event":"tool_call","ts":"2026-11-12 11:00:00"}\n')
        f.write(ev(600, "turn", agent="qa", note='said "event": "tool_call" in passing'))
    kinds = {e["event"] for e in Record(td)._events()}
    assert kinds == {"turn"}
    assert len(Record(td).events("turn")) == 7


def test_everything_can_still_be_asked_for(team):
    _, td = team
    rec = Record(td)
    assert len(rec.events()) == 306 and len(rec.events("tool_call")) == 300
    turns = rec.events("turn")
    turns[0]["agent"] = "changed"                   # a caller's copy, not the kept rows
    assert rec.events("turn")[0]["agent"] == "coder"


# --- a tool call is not news ---------------------------------------------------------

def test_a_tool_call_does_not_send_the_pages_to_look_again(team):
    _, td = team
    quiet, loud = Feed(td, tool_calls=False), Feed(td)
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(700) + ev(701) + ev(702, "turn", agent="qa") + ev(703))
    assert [i["item"]["event"] for i in quiet.poll()] == ["turn"]
    assert [i["item"]["event"] for i in loud.poll()] == ["tool_call", "tool_call", "turn", "tool_call"]
    server = (ROOT / "ateam/chat/server.py").read_text()
    assert "Feed(t.dir, tool_calls=False)" in server


def test_a_tool_call_does_not_have_the_board_counted_again(team, monkeypatch):
    root, td = team
    app = App(root, [], "roelle")
    app.tasks("demo")
    n = {"usage": 0}
    real = Record.task_usage

    def counting(self):
        n["usage"] += 1
        return real(self)
    monkeypatch.setattr(Record, "task_usage", counting)
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(800) + ev(801))
    app.tasks("demo"), app.state("demo")
    assert n["usage"] == 0, "a tool call had the tokens per task worked out again"
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(802, "turn", agent="qa"))
    app.tasks("demo")
    assert n["usage"] >= 1, "a turn did not"


# --- activity --------------------------------------------------------------------------

def test_activity_is_the_agents_latest_events_tool_calls_and_all(server):
    app, port, td = server
    st, out = call(port, "GET", "/api/activity?team=demo&agent=qa", token=app.token)
    assert st == 200
    rows = out["events"]
    assert len(rows) == 100 and {r["agent"] for r in rows} == {"qa"}
    assert rows[-1]["args"]["command"] == "pytest -k t297" and rows[0]["args"]["command"] == "pytest -k t0"
    assert all(r.get("t") for r in rows)
    st, out = call(port, "GET", "/api/activity?team=demo&agent=coder", token=app.token)
    whole = [e for e in Record(td).events() if e.get("agent") == "coder"][-120:]
    assert [(r["event"], r["ts"]) for r in out["events"]] == [(e["event"], e["ts"]) for e in whole]
    assert out["events"][-1]["event"] == "turn" and len(whole) == 120      # newest last


def test_activity_reads_the_end_of_the_log_not_all_of_it(team, monkeypatch):
    _, td = team
    with (td / "events.jsonl").open("a") as f:
        for i in range(1000, 1200):
            f.write(ev(i, agent="qa"))
    seen = parsed(monkeypatch)
    rows = Record(td).activity("qa", limit=20)
    assert [r["args"]["command"] for r in rows][-1] == "pytest -k t1199" and len(rows) == 20
    assert len(seen) == 20, f"parsed {len(seen)} lines for the last 20"


def test_the_log_read_backwards_is_every_line_once(tmp_path):
    p = tmp_path / "log.jsonl"
    rows = [{"n": i, "pad": "x" * (i * 7 % 90)} for i in range(200)]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"n": 200, "pa')     # one still being written
    for block in (16, 64, 100, 4096, 1 << 20):
        assert [r["n"] for r in logtail.newest(p, block=block)] == list(range(199, -1, -1)), block
    assert list(logtail.newest(tmp_path / "none.jsonl")) == []
    assert [r["n"] for r in logtail.newest(p, contains=b'"n": 7,')] == [7]


def test_an_agent_with_no_activity_has_none(team):
    _, td = team
    assert Record(td).activity("nobody") == [] and Record(td).activity("") == []


# --- asked together, answered once ---------------------------------------------------

def test_people_asking_together_share_one_answer():
    shared, n, gate = Shared(), {"runs": 0}, threading.Event()

    def compute():
        n["runs"] += 1
        mine = n["runs"]
        gate.wait(5)
        return mine
    got = []
    first = threading.Thread(target=lambda: got.append(shared.get(compute)))
    first.start()
    while n["runs"] < 1:
        time.sleep(0.005)
    rest = [threading.Thread(target=lambda: got.append(shared.get(compute))) for _ in range(8)]
    [t.start() for t in rest]
    time.sleep(0.1)                                 # all eight are waiting on the first
    gate.set()
    [t.join(5) for t in [first, *rest]]
    assert n["runs"] == 2, f"{n['runs']} computations for nine askers"
    assert sorted(got) == [1] + [2] * 8


def test_nobody_is_given_an_answer_older_than_their_question():
    """Someone who asks while one is being worked out may have just changed
    something it has already read past. They wait for the next."""
    shared, world, started, gate = Shared(), {"v": "old"}, threading.Event(), threading.Event()

    def slow():
        seen = world["v"]
        started.set()
        gate.wait(5)
        return seen
    got = {}
    a = threading.Thread(target=lambda: got.update(a=shared.get(slow)))
    a.start()
    started.wait(5)
    world["v"] = "new"                              # changed after the first began reading
    b = threading.Thread(target=lambda: got.update(b=shared.get(lambda: world["v"])))
    b.start()
    time.sleep(0.05)
    gate.set()
    a.join(5), b.join(5)
    assert got == {"a": "old", "b": "new"}


def test_a_failure_is_everyones_and_the_next_asker_starts_again():
    shared = Shared()

    def boom():
        raise LookupError("no team")
    with pytest.raises(LookupError):
        shared.get(boom)
    assert shared.get(lambda: 7) == 7


def test_the_state_is_shared_per_person_and_right_under_a_crowd(server):
    app, port, td = server
    n = {"runs": 0}
    real = app._state

    def counting(team):
        n["runs"] += 1
        time.sleep(0.2)
        return real(team)
    app._state = counting
    out = []
    ts = [threading.Thread(target=lambda: out.append(call(port, "GET", "/api/state?team=demo", token=app.token)))
          for _ in range(12)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert [s for s, _ in out] == [200] * 12
    assert n["runs"] <= 3, f"{n['runs']} computations for twelve requests at once"
    assert all(b["team"] == "demo" and len(b["agents"]) == 2 for _, b in out)


def test_a_burst_of_requests_is_not_turned_away_at_the_door(server):
    app, port, td = server
    httpd = serve(App(td.parent.parent, [], "roelle"), "127.0.0.1", 0)
    try:
        assert httpd.request_queue_size >= 128
    finally:
        httpd.server_close()


def test_a_page_looks_once_at_a_time():
    for page in ("app.js", "board.js"):
        src = (ROOT / "ateam/chat/static" / page).read_text()
        assert "if (looking) { again = true; return; }" in src, page


# --- the log itself ------------------------------------------------------------------

def test_the_signature_changes_with_a_turn_and_not_with_a_tool_call(team):
    _, td = team
    rec = Record(td)
    sig = rec.events_signature()
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(900))
    assert rec.events_signature() == sig
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(901, "turn"))
    assert rec.events_signature() != sig


def test_a_log_rewritten_is_read_again_and_never_repeats_a_signature(team):
    _, td = team
    rec, p = Record(td), td / "events.jsonl"
    sig = rec.events_signature()
    assert len(rec._events()) == 6
    p.write_text(ev(1, "turn") + ev(2, "turn"))                 # shorter
    assert len(rec._events()) == 2 and rec.events_signature() != sig
    sig = rec.events_signature()
    same_length = ev(1, "turn", agent="qa") + ev(2, "turn", agent="qa")
    p.write_text(same_length + ev(3))                           # the same turns' worth, other agents
    assert {e["agent"] for e in rec._events()} == {"qa"} and rec.events_signature() != sig
    p.unlink()
    assert rec._events() == [] and rec.events("turn") == []


def test_a_first_read_is_a_block_at_a_time_and_misses_nothing(team, monkeypatch):
    _, td = team
    monkeypatch.setattr(logtail, "BLOCK", 512)
    logtail.forget(td / "events.jsonl")
    assert len(Record(td)._events()) == 6
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(950, "turn")[:30])                           # a turn still being written
    assert len(Record(td)._events()) == 6
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(950, "turn")[30:])
    assert len(Record(td)._events()) == 7


def test_times_are_read_as_before(monkeypatch):
    for s in ("2026-11-12 19:55:01", "2026-03-08 02:30:00", "2026-11-01 01:30:00"):
        assert record.instant(s) == time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
        assert record._epoch(s) == record.instant(s)
    import calendar
    assert record.instant("2026-10-27T19:01:18Z") == calendar.timegm((2026, 10, 27, 19, 1, 18))
    for bad in (None, "", "soon", "2026-13-40 00:00:00", 12):
        assert record.instant(bad) is None and record._epoch(bad) is None


# --- the supervisor's reads --------------------------------------------------------

def test_the_supervisor_reads_turns_without_the_tool_calls(team, monkeypatch):
    _, td = team
    obs = FileObserver({"team_dir": str(td)})
    assert len(obs.events("turn")) == 6
    seen = parsed(monkeypatch)
    assert len(obs.events("turn")) == 6 and obs.events("failure") == []
    assert not seen, "the log was parsed again"
    obs.record_turn("qa", "c", 3.0)
    obs.record_tool_call("qa", "c", "Bash", args={"command": "ls"}, result="x")
    assert len(obs.events("turn")) == 7 and len(calls_in(seen)) == 0
    assert len(obs.events("tool_call")) == 301 and len(obs.events()) == 308


def test_an_agents_calls_since_a_time_are_read_from_the_end(team, monkeypatch):
    _, td = team
    obs = FileObserver({"team_dir": str(td)})
    since = "2026-11-12 10:04:40"                               # i = 280
    seen = parsed(monkeypatch)
    got = obs.calls("coder", since)
    n = len(seen)
    want = [e for e in Record(td).events("tool_call") if e["agent"] == "coder" and e["ts"] >= since]
    assert got == want and len(got) == 14
    assert n < 120, f"parsed {n} lines to find the last twenty seconds"
    assert obs.calls("coder") == [e for e in obs.events("tool_call") if e["agent"] == "coder"]
    assert obs.calls("nobody", since) == []


def test_calls_out_of_order_by_a_second_are_not_lost(team):
    _, td = team
    with (td / "events.jsonl").open("a") as f:
        f.write(ev(1000, agent="qa") + ev(999, "turn", agent="coder") + ev(1001, agent="qa"))
    got = FileObserver({"team_dir": str(td)}).calls("qa", "2026-11-12 10:16:40")
    assert [e["args"]["command"] for e in got] == ["pytest -k t1000", "pytest -k t1001"]


def test_the_review_gate_still_sees_the_work(team):
    _, td = team
    ok, evidence = activity.author_activity(td, "coder", "2026-11-12 10:00:00")
    assert ok and evidence == ["6 recorded turn(s)"]
    ok, evidence = activity.author_activity(td, "qa", "2026-11-12 10:00:00")
    assert ok and evidence == []
    with (td / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": "2026-11-12 11:00:00", "event": "episode_start"}) + "\n")
    assert activity.episode_start(td) == "2026-11-12 11:00:00"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
