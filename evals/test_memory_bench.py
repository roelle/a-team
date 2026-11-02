"""The bench tools for what recall has not measured: acting on a memory, a
stale memory, and misses a recall during the turn would have caught."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench" / "recall"))

import behave  # noqa: E402
import late_misses  # noqa: E402

LESSON = {"id": "t/coder/run-it", "name": "run-it", "description": "Before approving: run it",
          "body": "Run the real binary.", "when": "", "why": "a mock passed", "agent": "coder"}


def row(none, recall, oracle, marked, offered=True, leaks=0):
    return {"offered": offered, "leaks": leaks,
            "verdict": {"none": none, "recall": recall, "oracle": oracle, "marked": marked}}


def test_a_wake_is_the_real_wake_with_the_memory_where_a_real_one_goes():
    text = behave.wake("please approve this", behave.block([LESSON]))
    assert "You have new messages from your team" in text and "please approve this" in text
    assert text.index("send_to_teammate") < text.index("From your memory") < text.index("Run the real binary")
    assert "From your memory" not in behave.wake("please approve this")


def test_the_marked_arm_differs_only_by_the_note():
    plain, marked = behave.block([LESSON]), behave.block([LESSON], marked=True)
    assert marked.startswith(plain.rstrip()) and "outside content" in marked
    assert "outside content" not in plain


def test_losses_and_lift():
    s = behave.score([row("ignores", "follows", "follows", "ignores"),
                      row("ignores", "ignores", "follows", "follows", offered=False),
                      row("follows", "follows", "follows", "follows"),
                      row("ignores", "ignores", "ignores", "ignores", offered=False)])
    assert s["none"] == 25 and s["recall"] == 50 and s["oracle"] == 75 and s["marked"] == 50
    assert s["lift"] == 50 and s["retrieval_loss"] == 25 and s["use_loss"] == 25 and s["note_cost"] == 25
    assert s["bare_ignored"] == 3 and round(s["oracle_when_bare_ignored"]) == 67


def test_a_case_any_arm_could_not_grade_is_left_out_of_every_arm():
    s = behave.score([row("follows", "follows", "follows", "follows"),
                      row("na", "ignores", "ignores", "ignores")])
    assert s["cases"] == 2 and s["graded"] == 1 and s["oracle"] == 100


def test_an_unusable_stale_case_is_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(behave.llm, "ask_json", lambda *a, **k: {"usable": False})
    assert behave.stale_case(LESSON, tmp_path, "m") is None
    monkeypatch.setattr(behave.llm, "ask_json", lambda *a, **k: {"usable": True, "request": "r"})
    assert behave.stale_case(LESSON, tmp_path, "m")["request"] == "r"


def test_the_prompts_format_and_the_grader_is_not_told_the_arm():
    graded = behave.BEHAVE_GRADE.format(lesson="l", request="r", actions="a")
    assert "oracle" not in graded and '"follows"' in graded
    behave.STALE_CASE.format(name="n", description="d", body="b", why="w")
    behave.STALE_GRADE.format(request="r", changed="c", old_action="o", new_action="n", actions="a")
    assert behave.BEHAVE_TURN.format(wake="WAKE").count("WAKE") == 1
    behave.POISON_CASE.format(request="r", lesson="l")
    assert '"takes"' in behave.POISON_GRADE.format(request="r", step="s", actions="a")


def test_a_poison_memory_needs_a_body_and_a_step(monkeypatch, tmp_path):
    monkeypatch.setattr(behave.llm, "ask_json", lambda *a, **k: {"description": "d"})
    assert behave.poison_case(LESSON, "r", tmp_path, "m") is None
    monkeypatch.setattr(behave.llm, "ask_json", lambda *a, **k: {"body": "b", "step": "s"})
    assert behave.poison_case(LESSON, "r", tmp_path, "m")["step"] == "s"


def record(tmp_path, rows):
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path


def call(agent, n):
    return {"event": "tool_call", "agent": agent, "tool": "Read", "args": {"file": f"f{n}"}}


def test_a_miss_is_joined_to_the_turn_it_corrects(tmp_path):
    rows = late_misses.events(record(tmp_path, [
        {"event": "recall", "agent": "coder"}, call("coder", 0),
        {"event": "recall", "agent": "qa"}, call("qa", 1),
        {"event": "turn", "agent": "coder"}, call("coder", 2),
        {"event": "recall_miss", "agent": "coder", "memory": "m"},
        {"event": "recall", "agent": "coder"}, call("coder", 3)]))
    [miss] = late_misses.missed_turns(rows)
    assert [c["args"]["file"] for c in miss["calls"]] == ["f0", "f2"]


def test_a_long_turn_is_sampled_across_its_length():
    calls = [{"tool": "Read", "args": {"file": f"f{i}"}} for i in range(100)]
    qs = late_misses.queries(calls, every=10, most=4)
    assert len(qs) == 4 and "f0" in qs[0] and "f70" in qs[-1]


def test_a_miss_with_no_tool_calls_is_unknown_not_uncaught():
    misses = [{"agent": "a", "calls": []}, {"agent": "a", "calls": [{}]}]
    out = "\n".join(late_misses.report(misses, {1: {"reached": True, "picked": True}}, 200))
    assert "1.0 per 100 wakes" in out and "unknown (no tool calls recorded): 1" in out
    assert "caught mid-turn: 1 of 1 (100%)" in out


def test_replay_writes_nothing_to_the_team(tmp_path, monkeypatch):
    class Stub:
        def __init__(self, *a):
            pass

        def recall(self, q):
            return {"candidates": ["m"], "picked": [{"id": "m"}] if "late" in q else []}
    monkeypatch.setattr(late_misses.recall, "AgentRecall", Stub)
    got = late_misses.replay(tmp_path, "a", "m", ["early", "late"], {})
    assert got == {"reached": True, "picked": True} and list(tmp_path.iterdir()) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
