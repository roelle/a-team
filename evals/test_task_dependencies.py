"""Tasks that wait for other tasks, and tasks that expect to touch one path.

Asked for after reading another team harness's board: here, "do this after
that" lived in a note, so an agent had to remember to come back, and two agents
editing one directory found out from the merge.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import actions, mcp_tasks  # noqa: E402
from ateam import tasks as tl  # noqa: E402
from ateam.record import Record  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "builds"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    return td


def mail(agent):
    bus = load_transport(agent)
    try:
        return [m.content for m in bus.peek()]
    finally:
        bus.close()


def task(team, title="t", owner="coder", **kw):
    return tl.create(team, "", title, owner=owner, created_by=owner, **kw)["id"]


def test_a_waiting_task_cannot_be_started(team):
    first = task(team, "schema")
    second = tl.create(team, "", "api", created_by="qa", blocked_by=[first])["id"]
    with pytest.raises(tl.TaskError, match=f"waits for {first} \\(queued\\)"):
        tl.claim(team, second, "qa")
    third = task(team, "owned", blocked_by=[first])
    for status in ("running", "in_progress", "claimed"):
        with pytest.raises(tl.TaskError, match="waits for"):
            tl.update(team, third, "coder", status=status)


def test_it_can_still_be_noted_blocked_or_cancelled(team):
    first = task(team)
    second = task(team, blocked_by=[first])
    assert tl.update(team, second, "coder", status="blocked")["status"] == "blocked"
    assert tl.update(team, second, "coder", status="cancelled")["status"] == "cancelled"


def test_the_operator_can_start_it_anyway(team):
    first = task(team)
    second = task(team, blocked_by=[first])
    assert tl.update(team, second, "user", status="running")["status"] == "running"


def test_the_owner_hears_when_the_last_blocker_is_done(team):
    a, b = task(team, "a"), task(team, "b")
    waiter = task(team, "c", owner="qa", blocked_by=[a, b])
    tl.complete(team, a, "coder", "done")
    assert mail("qa") == []
    tl.complete(team, b, "coder", "done")
    assert len(mail("qa")) == 1 and f"{waiter}" in mail("qa")[0] and "is ready" in mail("qa")[0]
    assert tl.claim(team, waiter, "qa")["status"] == "claimed"


def test_a_blocker_that_fails_does_not_release_the_task(team):
    a = task(team, "a")
    waiter = task(team, "c", owner="qa", blocked_by=[a])
    tl.update(team, a, "coder", status="failed")
    assert "closed as failed" in mail("qa")[0] and "Decide" in mail("qa")[0]
    with pytest.raises(tl.TaskError, match="failed"):
        tl.claim(team, waiter, "qa")


def test_a_closed_task_hears_nothing(team):
    a = task(team, "a")
    waiter = task(team, "c", owner="qa", blocked_by=[a])
    tl.update(team, waiter, "qa", status="cancelled")
    tl.complete(team, a, "coder", "done")
    assert mail("qa") == []


def test_an_unowned_task_tells_whoever_made_it(team):
    a = task(team, "a")
    tl.create(team, "", "c", created_by="qa", blocked_by=[a])
    tl.complete(team, a, "coder", "done")
    assert "is ready" in mail("qa")[0]


def test_impossible_waits_are_refused(team):
    a = task(team, "a")
    b = task(team, "b", blocked_by=[a])
    c = task(team, "c", blocked_by=[b])
    with pytest.raises(tl.TaskError, match="no task 'nope'"):
        task(team, blocked_by=["nope"])
    with pytest.raises(tl.TaskError, match="cannot wait for itself"):
        tl.update(team, a, "coder", blocked_by=[a])
    with pytest.raises(tl.TaskError, match="would stop both"):
        tl.update(team, a, "coder", blocked_by=[c])
    assert tl.get(team, a).get("blocked_by") == []


def test_tasks_from_before_this_have_no_blockers(team):
    (team / "tasks.jsonl").write_text(json.dumps(
        {"id": "task_old", "kind": "created", "ts": "2026-01-01T00:00:00Z", "title": "old",
         "owner": "coder", "status": "queued"}) + "\n")
    assert tl.claim(team, "task_old", "coder")["status"] == "claimed"


def test_paths_are_compared_by_component(team):
    a = task(team, "a", writes=["./src/api/", "docs"])
    b = task(team, "b", owner="qa", writes=["src"])
    c = task(team, "c", owner="qa", writes=["src2", "doc"])
    state = tl.snapshot(team)
    assert tl.get(team, a)["writes"] == ["src/api", "docs"]
    assert [o["id"] for o in tl.overlaps(state, state[a])] == [b]
    assert tl.overlaps(state, state[c]) == []


def test_a_closed_task_no_longer_overlaps(team):
    a = task(team, "a", writes=["src"])
    b = task(team, "b", owner="qa", writes=["src"])
    tl.update(team, b, "qa", status="cancelled")
    state = tl.snapshot(team)
    assert tl.overlaps(state, state[a]) == []


def dispatch(agent, team, name, **args):
    captured = {}
    mcp_tasks.serve, keep = (lambda n, t, d, footer=None: captured.update(d=d)), mcp_tasks.serve
    try:
        mcp_tasks.main(agent, team_dir=team)
    finally:
        mcp_tasks.serve = keep
    return captured["d"](name, args)


def test_the_tools_take_lists_and_show_what_matters(team):
    a = task(team, "a", writes=["src"])
    out = dispatch("qa", team, "create_task", title="b", blocked_by=a, writes="src/x, docs")
    assert f"waits for {a}" in out
    assert "writes: src/x, docs" in out
    assert f"overlap: {a} (coder) expects to write src" in out
    tid = out.split("task created: ")[1].split("]")[0]
    refused = dispatch("qa", team, "claim_task", task_id=tid)
    assert refused.startswith("[error:") and "waits for" in refused
    cleared = dispatch("qa", team, "update_task", task_id=tid, blocked_by="none", writes="none")
    assert "waits for" not in cleared and "writes:" not in cleared
    assert tl.get(team, tid)["blocked_by"] == [] and tl.get(team, tid)["writes"] == []


def test_a_note_alone_leaves_the_lists_alone(team):
    a = task(team, "a")
    b = task(team, "b", blocked_by=[a], writes=["src"])
    dispatch("coder", team, "update_task", task_id=b, note="still planning")
    assert tl.get(team, b)["blocked_by"] == [a] and tl.get(team, b)["writes"] == ["src"]


def test_the_record_and_the_board_can_read_and_set_them(team):
    a = task(team, "a")
    t = actions.create_task(team, "", "from the board", blocked_by=[a], writes=["src"])
    assert t["blocked_by"] == [a]
    row = next(x for x in Record(team).tasks() if x["id"] == t["id"])
    assert row["blocked_by"] == [a] and row["writes"] == ["src"]
    assert actions.update_task(team, t["id"], blocked_by=[])["blocked_by"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
