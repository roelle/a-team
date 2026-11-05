"""A task links back to the discussion it came from: a channel and a thread.

Asked for: "For blocker tasks, they should have a channel and thread. An agent
made one for me - but I can't find what discussion it came from." These pin
that an agent's task is linked to the thread of the message it was
answering without the agent having to say so, that a linked task always
ends up with a thread (its first notice starts one), that the notices about
it are replies in that thread, that a task records the message it came
from even when that was a direct message, and that an agent cannot block a
task without saying where it is discussed.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import channels, heartbeat, mcp_tasks, tasks  # noqa: E402
from ateam.record import Record  # noqa: E402
from ateam.transport_file import FileTransport  # noqa: E402


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "builds"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    channels.create(td, "billing", ["coder", "qa"])
    channels.create(td, "docs", ["coder"])
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.delenv("ATEAM_BUS_TRANSPORT", raising=False)
    return td


def bus(td):
    return [json.loads(l) for l in (td / "bus.jsonl").read_text().splitlines() if l.strip()]


def post(td, who, chan, text, thread=None):
    return channels.post(FileTransport(who, {"team_dir": str(td)}), td, chan, text,
                         thread=thread)["id"]


def answering(td, agent, *ids):
    """`agent` is mid-turn on the messages `ids`, under a live supervisor."""
    rows = {r["id"]: r for r in bus(td)}
    heartbeat.path(td).write_text(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pid": os.getpid()}))
    (td / "inflight.json").write_text(json.dumps({agent: {
        "ts": "now", "msgs": [{**rows[i], "to": agent} for i in ids]}}))


def test_a_linked_task_gets_a_thread_and_its_notices_are_replies_in_it(team):
    t = tasks.create(team, "", "fix invoices", created_by="coder", channel="billing")
    first = bus(team)[-1]
    assert t["thread"] == first["id"] and first["task_id"] == t["id"]
    tasks.update(team, t["id"], "coder", status="running")
    assert bus(team)[-1]["thread"] == first["id"]


def test_a_task_from_a_thread_is_noted_in_that_thread(team):
    root = post(team, "user", "billing", "invoices are wrong")
    t = tasks.create(team, "", "fix invoices", created_by="coder", channel="billing",
                     thread=root)
    assert t["thread"] == root and bus(team)[-1]["thread"] == root


def test_an_agents_task_is_linked_to_the_post_it_was_answering(team):
    root = post(team, "user", "billing", "@coder invoices are wrong")
    reply = post(team, "qa", "billing", "I see it too", thread=root)
    answering(team, "coder", reply)
    out = mcp_tasks._create_task("coder", team, {"title": "fix invoices"})
    t = next(iter(tasks.snapshot(team).values()))
    assert t["channel"] == "billing" and t["thread"] == root, out
    assert t["origin"]["id"] == reply and t["origin"]["from"] == "qa"
    assert f"discussed in #billing, thread {root}" in out


def test_a_task_from_a_direct_message_records_where_it_came_from(team):
    FileTransport("user", {"team_dir": str(team)}).send("coder", "please chase the vendor")
    dm = bus(team)[-1]["id"]
    answering(team, "coder", dm)
    mcp_tasks._create_task("coder", team, {"title": "chase vendor", "channel": "billing"})
    t = next(iter(tasks.snapshot(team).values()))
    assert t["origin"] == {"id": dm, "from": "user", "to": "coder"}
    assert t["channel"] == "billing" and t["thread"] == bus(team)[-1]["id"], \
        "named channel, no thread there: the first notice starts one"


def test_an_agent_that_blocks_a_task_with_no_channel_is_told_where_to_say(team):
    t = tasks.create(team, "", "ship", created_by="coder", owner="coder")
    out = mcp_tasks._update_task("coder", team, {"task_id": t["id"], "status": "blocked"})
    assert tasks.get(team, t["id"])["status"] == "blocked", "waiting on its own sim is fine"
    assert "blocked with no channel" in out and "#billing" in out


def test_a_blocked_tasks_discussion_is_one_click_from_needs_you(team):
    root = post(team, "user", "docs", "@coder can you ship?")
    t = tasks.create(team, "", "ship", created_by="coder", owner="coder",
                     channel="docs", thread=root)
    tasks.update(team, t["id"], "coder", status="blocked")
    row = next(r for r in Record(team).needs_you() if r.get("id") == t["id"])
    assert (row["kind"], row["channel"], row["thread"]) == ("stuck", "docs", root)


def test_blocking_mid_conversation_links_the_conversation(team):
    t = tasks.create(team, "", "ship", created_by="coder", owner="coder")
    root = post(team, "user", "docs", "@coder need the API key from you-know-who")
    answering(team, "coder", root)
    mcp_tasks._update_task("coder", team, {"task_id": t["id"], "status": "blocked"})
    got = tasks.get(team, t["id"])
    assert (got["status"], got["channel"], got["thread"]) == ("blocked", "docs", root)
    assert bus(team)[-1]["thread"] == root


def test_a_channel_change_starts_over_in_the_new_channel(team):
    t = tasks.create(team, "", "fix", created_by="coder", channel="billing")
    tasks.update(team, t["id"], "coder", channel="docs")
    got = tasks.get(team, t["id"])
    assert got["channel"] == "docs" and got["thread"] == bus(team)[-1]["id"] != t["thread"]
    tasks.update(team, t["id"], "coder", channel="")
    assert tasks.get(team, t["id"])["thread"] is None


def test_the_app_is_sent_the_link():
    from ateam.chat.server import App
    assert {"thread", "origin"} <= set(App.TASK_FIELDS)


def test_the_board_and_the_chat_follow_the_link():
    board = (ROOT / "ateam/chat/static/board.js").read_text()
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    assert "function discussionURL" in board and "&thread=" in board and "originHTML(t.origin)" in board
    assert "linkedThread()" in app and "origin:" in app and "function talkLink" in app


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
