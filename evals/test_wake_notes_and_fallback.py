"""What the roster adds to a wake, and where a turn goes when its model is full.

Asked for: a reminder, set in the roster, on every prompt the operator
sends an agent directly; a one-time nudge to save memory once a
conversation passes N tokens; and on a 429 or a 503, the next permitted
model for a while instead of the same one again.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import quota, runner as runner_lib  # noqa: E402
from ateam.record import Record  # noqa: E402
from ateam.supervisor import Supervisor  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402
from fixture_runner import ScriptedRunner  # noqa: E402

AGENTS = ["lead", "coder"]


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "lead", "role": "coordinates", "model": "big"},
        {"name": "coder", "role": "builds", "model": "big"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "0")
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.setattr(runner_lib, "RETRY_BASE_SECONDS", 0.0)
    return td


def roster(team, **top):
    doc = json.loads((team / "roster.json").read_text())
    doc.update(top)
    (team / "roster.json").write_text(json.dumps(doc))
    return doc


class Sees(ScriptedRunner):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.prompts = []

    def wake(self, agent, message):
        self.prompts.append((agent, message))
        return "ok"


def run(team, sends, runner):
    for sender, to, text in sends:
        load_transport(sender).send(to, text)
    sup = Supervisor(AGENTS, runner, quiet=True, require_review=False)
    try:
        sup.run_until_idle()
    finally:
        sup.close()


# --- the operator's reminder ------------------------------------------------------

def test_a_prompt_from_the_operator_carries_the_rosters_reminder(team):
    roster(team, operator_reminder="Answer me in the thread I asked in.")
    r = Sees({})
    run(team, [("user", "coder", "what broke?")], r)
    assert "[Reminder from the operator] Answer me in the thread I asked in." in r.prompts[-1][1]
    run(team, [("lead", "coder", "please look at the build")], r)
    assert "Reminder from the operator" not in r.prompts[-1][1], "a teammate's message is not the operator's"


def test_an_agent_can_have_its_own_reminder_or_none(team):
    doc = roster(team, operator_reminder="Say which branch you are on.")
    doc["agents"][1]["operator_reminder"] = ""
    (team / "roster.json").write_text(json.dumps(doc))
    r = Sees({})
    run(team, [("user", "coder", "status?"), ("user", "lead", "status?")], r)
    seen = dict(r.prompts)
    assert "Reminder from the operator" not in seen["coder"]
    assert "Say which branch you are on." in seen["lead"]


# --- the nudge to save memory -------------------------------------------------------

def test_past_the_rosters_size_an_agent_is_asked_once_to_save_memory(team, monkeypatch):
    monkeypatch.setenv("ATEAM_CYCLE_THRESHOLD", "10000000")   # this asks before distilling would
    roster(team, memory_nudge_tokens=100_000)
    (team / "conversations.json").write_text(json.dumps({"coder": "conv-1"}))
    r = Sees({})
    r.observer.record_turn("coder", "conv-1", 1.0, input_tokens=10, context_tokens=50_000)
    run(team, [("lead", "coder", "one")], r)
    assert "save_memory" not in r.prompts[-1][1], "under the size, no nudge"
    r.observer.record_turn("coder", "conv-1", 1.0, input_tokens=10, context_tokens=140_000)
    run(team, [("lead", "coder", "two")], r)
    assert "holds 140,000 tokens" in r.prompts[-1][1] and "save_memory" in r.prompts[-1][1]
    run(team, [("lead", "coder", "three")], r)
    assert "save_memory" not in r.prompts[-1][1], "once per conversation"
    (team / "conversations.json").write_text(json.dumps({"coder": "conv-2"}))
    r.observer.record_turn("coder", "conv-2", 1.0, input_tokens=10, context_tokens=150_000)
    run(team, [("lead", "coder", "four")], r)
    assert "save_memory" in r.prompts[-1][1], "a new conversation is nudged again"


# --- falling back on a 429 or a 503 --------------------------------------------------

class Limited(ScriptedRunner):
    """Turns on "big" are refused for capacity; anything else runs."""

    def __init__(self, *a, full=("big",), error="[error: 429 Too Many Requests: rate limit]", **k):
        super().__init__(*a, **k)
        self.full, self.error, self.ran = set(full), error, []

    def wake(self, agent, message):
        def turn():
            m, _ = self.resolved_model(agent, "big")
            self.ran.append((agent, m))
            return self.error if m in self.full else "ok"
        return runner_lib.with_retry(turn, attempts=3,
                                     on_capacity=lambda e: self.cool_down(agent, e))


def test_a_full_model_hands_its_agents_to_the_next_one_for_a_while(team):
    roster(team, fallback_models=["medium", "small"], fallback_cooldown=300)
    r = Limited({})
    assert r.wake("coder", "go") == "ok"
    assert r.ran == [("coder", "big"), ("coder", "medium")], "moved at once, no second try on big"
    assert r.wake("lead", "go") == "ok" and r.ran[-1] == ("lead", "medium"), \
        "the cool-down is the team's: every agent on big moves"
    q = Record(team).models()["coder"]["quota"]
    assert q["cooling_until"] and q["low"] is True
    doc = json.loads((team / quota.COOLDOWNS).read_text())
    doc["big"]["until"] = time.time() - 1
    (team / quota.COOLDOWNS).write_text(json.dumps(doc))
    r.full = set()
    r.wake("coder", "go")
    assert r.ran[-1] == ("coder", "big"), "after the cool-down it is back on its own model"


def test_the_next_model_skips_one_that_is_cooling_too(team):
    roster(team, fallback_models=["medium", "small"])
    r = Limited({}, full=("big", "medium"))
    assert r.wake("coder", "go") == "ok"
    assert [m for _, m in r.ran] == ["big", "medium", "small"]


def test_with_no_fallback_named_the_same_model_is_retried(team):
    r = Limited({})
    r.wake("coder", "go")
    assert [m for _, m in r.ran] == ["big", "big", "big"]
    assert not (team / quota.COOLDOWNS).exists()


def test_a_failure_that_is_not_about_capacity_moves_nothing(team):
    roster(team, fallback_models=["small"])
    r = Limited({}, error="[error: request timeout after 600s]")
    r.wake("coder", "go")
    assert {m for _, m in r.ran} == {"big"}


def test_an_agent_can_name_its_own_fallbacks(team):
    doc = roster(team, fallback_models=["medium"])
    doc["agents"][1]["fallback_models"] = ["small"]
    (team / "roster.json").write_text(json.dumps(doc))
    r = Limited({}, error="[error: 503 Service Unavailable]")
    r.wake("coder", "go")
    assert r.ran[-1] == ("coder", "small")


def test_the_supervisor_falls_back_on_a_failed_turn_without_backing_off(team):
    """A runner with no retry of its own (agy, a host) reports the 503 to the
    supervisor, which cools the model and wakes the agent again soon."""
    roster(team, fallback_models=["small"])

    class Once(Limited):
        def wake(self, agent, message):
            m, _ = self.resolved_model(agent, "big")
            self.ran.append((agent, m))
            return self.error if m in self.full else "ok"
    r = Once({}, error="[error: 503 the model is overloaded]")
    load_transport("user").send("coder", "go")
    sup = Supervisor(AGENTS, r, quiet=True, require_review=False)
    try:
        sup.run_until_idle()
        assert quota.cooling(team, "big")
        assert r.ran == [("coder", "big"), ("coder", "small")], "the mail went to the next turn, on small"
    finally:
        sup.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
