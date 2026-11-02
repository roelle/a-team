"""Did a memory offered on a real turn shape what the agent did?

The bench asks this of a model on prompts built for it. These two signals ask
it of real turns: a correction restating a memory the agent had been shown,
which needs no extra model call, and a judge on a sample of turns.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import config, recall  # noqa: E402
from ateam.record import Record  # noqa: E402
from ateam.supervisor import Supervisor  # noqa: E402
from fixture_runner import ScriptedRunner  # noqa: E402

AGENT = "coder"
MEMORY = {"id": "run-it", "description": "Before approving: run it", "body": "Run the real binary."}


class Backend:
    available = True

    def __init__(self, verdict="used"):
        self.verdict, self.asked = verdict, []

    def ask(self, prompt):
        self.asked.append(prompt)
        return {"verdict": self.verdict, "evidence": "ran ./app"}


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [{"name": AGENT, "role": "x"}],
                                                "memory_recall": {"used_sample": 1}}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    return td


@pytest.fixture
def sup(team):
    s = Supervisor([AGENT], ScriptedRunner({}), require_review=False, quiet=True)
    yield s
    s.close()


def offered(sup):
    sup._last_recall[AGENT] = {"query": "approve this", "picked": [MEMORY], "shown": {MEMORY["id"]}}


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def checks(team):
    return Record(team).recall_stats(AGENT)["checks"]


def test_a_sampled_turn_is_checked_against_what_it_did(team, sup, monkeypatch):
    backend = Backend()
    monkeypatch.setattr(recall, "backend_for", lambda cfg: backend)
    offered(sup)
    sup.observer.record_tool_call(AGENT, "c", "Bash", {"command": "./app --check"})
    sup._check_used(AGENT, {"ts": "2000-01-01 00:00:00"}, "approved after running it")
    assert wait_for(lambda: checks(team)["used"] == 1)
    assert "./app --check" in backend.asked[0] and "approved after running it" in backend.asked[0]
    assert "Run the real binary" in backend.asked[0] and "approve this" in backend.asked[0]
    assert Record(team).recall_stats(AGENT)["memories"]["run-it"]["used"] == 1


def test_a_turn_with_no_record_is_not_given_a_verdict(team, sup, monkeypatch):
    backend = Backend()
    monkeypatch.setattr(recall, "backend_for", lambda cfg: backend)
    offered(sup)
    sup._check_used(AGENT, {"ts": "2000-01-01 00:00:00"}, "")
    sup._pool.shutdown(wait=True)
    sup._pool = None
    assert backend.asked == [] and sum(checks(team).values()) == 0


def test_a_sample_of_zero_costs_nothing(team, sup, monkeypatch):
    backend = Backend()
    monkeypatch.setattr(recall, "backend_for", lambda cfg: backend)
    (team / "roster.json").write_text(json.dumps({"agents": [{"name": AGENT, "role": "x"}],
                                                  "memory_recall": {"used_sample": 0}}))
    offered(sup)
    sup._check_used(AGENT, {"ts": "x"}, "reply")
    assert backend.asked == []


def test_each_offer_is_checked_once(team, sup, monkeypatch):
    backend = Backend()
    monkeypatch.setattr(recall, "backend_for", lambda cfg: backend)
    offered(sup)
    sup._check_used(AGENT, {"ts": "2000-01-01 00:00:00"}, "done")
    sup._check_used(AGENT, {"ts": "2000-01-01 00:00:00"}, "done")
    assert wait_for(lambda: checks(team)["used"] == 1)
    sup._pool.shutdown(wait=True)
    sup._pool = None
    assert len(backend.asked) == 1


def test_a_long_turn_keeps_its_end():
    calls = [{"tool": "Read", "args": {"f": f"f{i}"}} for i in range(1000)]
    text = recall.turn_actions(calls, "done")
    assert len(text) < recall.USED_CHARS + 100 and '"f999"' in text and text.endswith("done")


def test_a_correction_of_a_memory_it_was_shown_counts_as_ignored(team, sup):
    from ateam.transport import Message

    class Ar:
        backend = Backend()

        def candidates(self, text, k):
            return [{"id": "run-it", "name": "run-it", "description": "d", "body": "b"}]

        def learn(self, *a):
            raise AssertionError("the words were not what failed: it was shown")
    Ar.backend.ask = lambda prompt: {"restates": "run-it"}
    offered(sup)
    sup._check_miss(AGENT, Ar(), [Message(ts="t", sender="user", to=AGENT, content="run it, as I said")])
    stats = Record(team).recall_stats(AGENT)
    assert [i["memory"] for i in stats["ignored"]] == ["run-it"] and stats["misses"] == []
    assert stats["memories"]["run-it"]["ignored"] == 1


def test_running_out_of_ai_studio_credit_stops_the_team_instead_of_looking_like_a_rate_limit():
    error = ("[error: 429 RESOURCE_EXHAUSTED. Your prepayment credits are depleted. "
             "Please go to AI Studio to manage your project and billing.]").lower()
    assert any(p in error for p in config.BUDGET_HALT_PATTERNS)
    assert not any(p in "[error: 429 resource_exhausted: rate limit]" for p in config.BUDGET_HALT_PATTERNS)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
