"""Editing and deleting what the operator sent, when the reader is an agent.

Asked for: edit and delete, as other chat apps have them. The catch is that
a message here is a prompt: an agent may have acted on it. So each recipient
gets what is true for it -- an unread copy changed in place, or a note with
the change for one that already read it -- and the record keeps both.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import actions  # noqa: E402
from ateam.record import Record  # noqa: E402
from ateam.transport_file import FileTransport  # noqa: E402


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "builds"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.delenv("ATEAM_BUS_TRANSPORT", raising=False)
    return td


def last_id(td):
    return json.loads((td / "bus.jsonl").read_text().splitlines()[-1])["id"]


def inbox(td, agent):
    p = td / "inbox" / f"{agent}.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def read(agent):
    t = FileTransport(agent)
    t.acknowledge(t.fetch())


def shown(td, mid):
    return next(r for r in Record(td).bus() if r.get("id") == mid)


def test_an_unread_message_is_changed_where_it_waits(team):
    actions.dm(team, "coder", "build the parser")
    mid = last_id(team)
    out = actions.edit_message(team, mid, "build the lexer")
    assert out["changed_unread"] == ["coder"] and out["told"] == []
    assert [m["content"] for m in inbox(team, "coder")] == ["build the lexer"]
    assert shown(team, mid)["content"] == "build the lexer" and shown(team, mid)["edited"]
    assert json.loads((team / "bus.jsonl").read_text().splitlines()[-1])["content"] == \
        "build the parser", "bus.jsonl keeps it as sent"


def test_a_message_already_read_is_corrected_with_a_note(team):
    actions.dm(team, "coder", "build the parser")
    mid = last_id(team)
    read("coder")
    out = actions.edit_message(team, mid, "build the lexer")
    assert out["told"] == ["coder"] and out["changed_unread"] == []
    note = inbox(team, "coder")[-1]
    assert note["note_of"] == mid and note["thread"] == mid and not note.get("quiet")
    assert "Act on this version instead" in note["content"] and "build the lexer" in note["content"]
    rows = [r for r in Record(team).bus() if r.get("note_of")]
    assert rows == [], "the note goes to the agent, not into the conversation you see"


def test_one_being_read_in_a_turn_now_counts_as_read(team):
    actions.dm(team, "coder", "build the parser")
    mid = last_id(team)
    (team / "inflight.json").write_text(json.dumps({"coder": {"msgs": [inbox(team, "coder")[0]]}}))
    out = actions.edit_message(team, mid, "build the lexer")
    assert out["told"] == ["coder"]
    assert inbox(team, "coder")[0]["content"] == "build the parser", "not changed under a running turn"


def test_delete_takes_an_unread_message_back_and_tells_a_reader_to_disregard_it(team):
    actions.dm(team, "qa", "review PR 12")
    mid = last_id(team)
    out = actions.delete_message(team, mid)
    assert out["changed_unread"] == ["qa"] and inbox(team, "qa") == []
    assert shown(team, mid)["deleted"] and shown(team, mid)["content"] == ""
    with pytest.raises(actions.OperatorError, match="deleted"):
        actions.edit_message(team, mid, "again")
    actions.dm(team, "qa", "review PR 13")
    mid = last_id(team)
    read("qa")
    out = actions.delete_message(team, mid)
    assert out["told"] == ["qa"] and "Disregard" in inbox(team, "qa")[-1]["content"]


def test_a_channel_post_reaches_each_member_as_it_reached_them(team):
    actions.post(team, "general", "@coder please look at the build")
    mid = last_id(team)
    read("qa")                                   # qa read its quiet copy; coder has not
    out = actions.edit_message(team, mid, "@coder please look at the deploy")
    assert out["changed_unread"] == ["coder"] and out["told"] == ["qa"]
    assert inbox(team, "coder")[0]["content"] == "@coder please look at the deploy"
    assert inbox(team, "qa")[-1].get("quiet") is True, "the note is as quiet as the post was for qa"


def test_only_what_you_sent(team):
    FileTransport("coder").send("user", "done")
    with pytest.raises(actions.OperatorError, match="only what you sent"):
        actions.edit_message(team, last_id(team), "not done")
    actions.dm(team, "coder", "hello", author="ann")
    mid = last_id(team)
    with pytest.raises(actions.OperatorError, match="only they"):
        actions.edit_message(team, mid, "bye", by="bob")
    actions.dm(team, "coder", "from the terminal")
    with pytest.raises(actions.OperatorError, match="only they"):
        actions.delete_message(team, last_id(team), by="bob", own_only=True)
    with pytest.raises(actions.OperatorError, match="nothing changed"):
        actions.edit_message(team, mid, "hello", by="ann")


def test_an_agent_cannot_edit_the_operators_messages(team, monkeypatch):
    actions.dm(team, "coder", "build it")
    monkeypatch.setenv("ATEAM_AGENT", "coder")
    with pytest.raises(actions.OperatorError, match="operator"):
        actions.edit_message(team, last_id(team), "skip it")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
