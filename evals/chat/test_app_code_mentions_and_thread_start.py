"""Two things about reading, and one about what is written down.

"Ignore code spans in @mention detection: backticked handles in technical
notes light phantom red @ badges." A handle in backticks is being written
about. These pin that it wakes nobody, marks nothing as mentioning you, in
a fenced block or an inline span, and that a mention recorded from one by
earlier code is not believed.

"When I select a message in a channel with unread messages in the thread,
the 3rd pane should start at the unread." These pin that the page marks
where the unread replies begin and starts the pane there.

The event log kept up to 20,000 characters of what every tool call
returned. These pin that it keeps the start and the end of a result, an
error and each argument, and says how much it left out.
"""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ateam import actions, channels, config
from ateam.chat.server import App, _handles_re, mentions_me, serve
from ateam.observer_file import FileObserver, clip
from ateam.record import Record
from ateam.transport_file import FileTransport

from test_app_api import call

ROOT = Path(__file__).resolve().parents[2]
FENCE = "```"


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


# --- a handle in code is not a mention ------------------------------------------

def test_code_is_left_out_of_what_is_said():
    assert channels.handles_in("ask @qa, not `@coder`") == ["qa"]
    assert channels.handles_in(f"log:\n{FENCE}\n@coder failed\n{FENCE}\nthen @qa") == ["qa"]
    assert channels.handles_in(f"never closed {FENCE}sh\n@coder @qa") == []
    assert channels.handles_in("a lone ` and then @qa") == ["qa"]
    assert channels.handles_in("`@a` @b `@c` @d") == ["b", "d"]
    assert channels.handles_in("`one\n@qa two`") == ["qa"]         # a span does not cross lines
    assert channels.handles_in(None) == []


def test_a_backticked_handle_wakes_nobody(server):
    app, port, td = server
    out = actions.post(td, "billing", "the regex reads `@coder` as a handle", author="roelle")
    assert out["woke"] == []
    row = next(r for r in Record(td).bus() if r["id"] == out["id"])
    assert "coder" not in (row.get("mentions") or [])
    out = actions.post(td, "billing", "@coder please look", author="roelle")
    assert out["woke"] == ["coder"]


def test_a_backticked_handle_in_a_direct_message_brings_nobody_in(server):
    app, port, td = server
    actions.dm(td, "coder", "the docs say `@qa` wakes qa", author="roelle")
    assert not (td / "inbox" / "qa.jsonl").exists()
    actions.dm(td, "coder", "@qa can you check this too?", author="roelle")
    assert (td / "inbox" / "qa.jsonl").exists()


def test_a_backticked_handle_of_yours_marks_nothing(server):
    app, port, td = server
    channels.post(FileTransport("coder"), td, "billing",
                  "mentions are matched with `@roelle` or `@user` in the text")
    channels.post(FileTransport("qa"), td, "release", f"{FENCE}\nto: @roelle\n{FENCE}")
    st = state(app, port)
    assert st["mentions_of_user"] == {}
    channels.post(FileTransport("qa"), td, "release", "and `@user` aside, @roelle ok to ship?")
    assert "c:release" in state(app, port)["mentions_of_user"]


def test_a_mention_recorded_from_code_is_not_believed():
    handles = {"user", "roelle"}
    said = _handles_re(handles)
    recorded = {"mentions": ["user"], "from": "coder"}
    # recorded by earlier code from a handle in backticks
    assert not mentions_me({**recorded, "content": "matched with `@roelle`"}, handles, said)
    # recorded, and said outright
    assert mentions_me({**recorded, "content": "@roelle see `@roelle`"}, handles, said)
    # recorded under a handle the roster no longer has: the record stands
    assert mentions_me({**recorded, "content": "@matt which rounding?"}, handles, said)
    assert mentions_me({**recorded, "content": "which rounding?"}, handles, said)
    assert not mentions_me({"content": "`@user`", "from": "coder"}, handles, said)


def test_the_page_leaves_code_out_too():
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    assert "const prose = (s) => (s || \"\").replace(CODE_RE, \" \");" in app
    assert "if (namesMe(prose(m.content))) return true;" in app
    assert "(m.mentions || []).includes(\"user\") && !namesMe(m.content || \"\")" in app


# --- a thread opens where the unread begins ---------------------------------------

def test_the_page_starts_a_thread_at_its_first_unread_reply():
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    body = app[app.index("async function openThread(id)"):][:4500]
    # what was unread is worked out before the thread is marked read
    assert body.index("const since = S.tseen[tKey(id)]") < body.index("S.tseen[tKey(id)] = last")
    assert "m.id !== id && m.from !== \"user\" && m.ts > since" in body
    assert '<div class="new-divider"><span>new</span></div>' in body
    # only when opened, not when drawn again for a reply that arrived
    assert "if (opening) {" in body and "box.scrollTop = mark ?" in body


# --- what the log keeps of a call --------------------------------------------------

def test_a_long_result_is_kept_at_its_start_and_its_end(tmp_path):
    obs = FileObserver({"team_dir": str(tmp_path)})
    out = "START" + "x" * 30_000 + "END: 3 failed"
    obs.record_tool_call("coder", "c", "Bash", args={"command": "pytest"}, result=out)
    obs.record_tool_call("coder", "c", "Bash", args={"command": "ls"}, result="short")
    long, short = obs.events("tool_call")
    assert short["result"] == "short"
    kept = long["result"]
    assert kept.startswith("START") and kept.endswith("END: 3 failed")
    assert f"[{len(out) - config.TOOL_RESULT_LOG_CHARS} chars left out]" in kept
    assert len(kept) < config.TOOL_RESULT_LOG_CHARS + 60
    line = (tmp_path / "events.jsonl").read_text().splitlines()[0]
    assert len(line) < 2_000, "a call still takes kilobytes of the log"


def test_an_error_keeps_more_and_arguments_are_clipped_each(tmp_path):
    obs = FileObserver({"team_dir": str(tmp_path)})
    err = "Traceback\n" + "frame\n" * 5_000 + "ValueError: bad rounding"
    obs.record_tool_call("coder", "c", "Write", error=err, result=None,
                         args={"file_path": "/w/a.py", "content": "y" * 50_000,
                               "edits": [{"old": "o" * 9_000, "new": "n"}], "count": 3})
    ev = obs.events("tool_call")[0]
    assert ev["error"].startswith("Traceback") and ev["error"].endswith("ValueError: bad rounding")
    assert config.TOOL_RESULT_LOG_CHARS < len(ev["error"]) < config.TOOL_ERROR_LOG_CHARS + 60
    a = ev["args"]
    assert a["file_path"] == "/w/a.py" and a["count"] == 3 and a["edits"][0]["new"] == "n"
    assert len(a["content"]) < config.TOOL_ARG_LOG_CHARS + 60 and "chars left out" in a["content"]
    assert len(a["edits"][0]["old"]) < config.TOOL_ARG_LOG_CHARS + 60
    assert ev["result"] is None


def test_what_reads_the_log_still_finds_what_it_reads(tmp_path):
    """The command's start (which trigger learning shapes a call by), a
    held call's marker and a memory's name all sit where clipping keeps them."""
    from ateam import trigger_learn, triggers
    obs = FileObserver({"team_dir": str(tmp_path)})
    cmd = "kubectl apply -f - <<EOF\n" + "spec: x\n" * 2_000 + "EOF"
    obs.record_tool_call("coder", "c", "Bash", args={"command": cmd},
                         error="error: unable to recognize STDIN: no matches for kind Deployment " + "z" * 9_000)
    obs.record_tool_call("coder", "c", "read_memory", args={"name": "kubectl-apply"}, result="m" * 5_000)
    obs.record_tool_call("coder", "c", "Bash", args={"command": "ls"},
                         error="[held: this call matches a memory] " + "t" * 9_000)
    first, read, held = obs.calls("coder")
    assert triggers.shape(first["tool"], first["args"]) == "Bash(kubectl apply)"
    assert trigger_learn.failed(first) and trigger_learn._held(held)
    assert read["args"]["name"] == "kubectl-apply"


def test_clip_says_what_it_left_out():
    assert clip("abc", 10) == "abc" and clip("", 10) == ""
    out = clip("a" * 600 + "b" * 600, 400)
    assert out.startswith("a" * 300) and out.endswith("b" * 100) and "[800 chars left out]" in out
    assert clip("x" * 50, 0) == "x" * 50                        # no limit set


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
