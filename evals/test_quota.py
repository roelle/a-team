"""How much quota a model has left, and what it costs.

Asked for: optional "remaining" (0-1), "resets_at" and "cost" on each model a
runner lists, filled in by each runner from what it knows, and a badge on an
agent whose model is low. A host's own runner can supply them too.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claude_agent_sdk import RateLimitInfo  # noqa: E402

from ateam import quota, runner as runner_lib  # noqa: E402
from ateam.record import Record  # noqa: E402
from ateam.runner_claude import ClaudeRunner  # noqa: E402
from ateam.runner_codex import CodexRunner  # noqa: E402

LATER = time.time() + 3600


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "builds", "model": "claude-opus-5-5"},
        {"name": "qa", "role": "reviews", "model": "claude-sonnet-5-5"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "0")
    return td


def test_the_tightest_live_limit_is_the_one_shown(team):
    quota.note(team, "claude", "claude-opus-5-5", "five_hour", remaining=0.6, resets_at=LATER)
    quota.note(team, "claude", "claude-opus-5-5", "seven_day_opus", remaining=0.1,
               resets_at=LATER + 86400, applies="opus")
    quota.note(team, "claude", "claude-sonnet-5-5", "seven_day", remaining=0.9)
    opus = quota.for_model(team, "claude-opus-5-5")
    assert opus["remaining"] == 0.1 and opus["window"] == "seven_day_opus"
    assert opus["resets_at"].startswith(time.strftime("%Y-"))
    sonnet = quota.for_model(team, "claude-sonnet-5-5")
    assert sonnet["remaining"] == 0.6, "an opus-only limit was applied to sonnet"
    assert quota.for_model(team, "gpt-6-luna") == {}, "another account's limit was applied"


def test_a_limit_past_its_reset_is_dropped(team):
    quota.note(team, "claude", "claude-opus-5-5", "five_hour", remaining=0.05, resets_at=time.time() - 5)
    assert quota.for_model(team, "claude-opus-5-5") == {}


def test_a_sparse_update_keeps_what_it_does_not_mention(team):
    quota.note(team, "codex", "gpt-6-luna", "primary", remaining=0.5, resets_at=LATER)
    quota.note(team, "codex", "gpt-6-luna", "primary", remaining=0.4)
    q = quota.for_model(team, "gpt-6-luna")
    assert q["remaining"] == 0.4 and q["resets_at"]


def test_listed_models_carry_cost_and_quota_and_keep_their_own_fields(team):
    quota.note(team, "claude", "claude-opus-5-5", "five_hour", remaining=0.3, resets_at=LATER)
    out = quota.annotate([{"id": "claude-opus-5-5", "label": "Opus"},
                          {"id": "claude-haiku-5-5", "label": "Haiku", "remaining": 0.8},
                          {"id": "unpriced", "label": "?"}], team, "claude")
    assert out[0]["remaining"] == 0.3 and out[0]["cost"] == {"input": 4.0, "output": 20.0}
    assert out[1]["remaining"] == 0.8, "a runner's own figure was overwritten"
    assert "cost" not in out[2] and out[2]["remaining"] == 0.3, "the account's limit is every listed model's"


def test_a_host_lists_its_own_quota_and_the_listing_adds_what_turns_saw(team, monkeypatch):
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "1")
    doc = json.loads((team / "roster.json").read_text())
    doc["supervisor"] = {"runner": "ateam.runner_host:HostRunner", "runner_config": {"command": "true", "models": [
        {"id": "big", "label": "Big", "remaining": 0.25, "resets_at": "2026-11-04T09:00:00-08:00",
         "cost": {"credits": 3}},
        "small"]}}
    (team / "roster.json").write_text(json.dumps(doc))
    runner_lib._MODEL_CACHE.clear()
    quota.note(team, "host", "small", "daily", remaining=0.5)
    listed = {m["id"]: m for m in runner_lib.list_models(team_dir=team)}
    assert listed["big"]["remaining"] == 0.25 and listed["big"]["cost"] == {"credits": 3}
    assert listed["small"]["remaining"] == 0.5


def test_the_claude_runner_records_its_subscription_limits(team):
    r = ClaudeRunner({})
    r._quota("claude-opus-5-5", RateLimitInfo(status="allowed_warning", resets_at=int(LATER),
                                              rate_limit_type="five_hour", utilization=0.85))
    r._quota("claude-opus-5-5", RateLimitInfo(status="allowed", rate_limit_type="seven_day_sonnet",
                                              utilization=0.99))
    q = quota.for_model(team, "claude-opus-5-5")
    assert q["window"] == "five_hour" and abs(q["remaining"] - 0.15) < 1e-6
    assert q["status"] == "allowed_warning"


def test_the_codex_runner_records_its_account_limits_and_credits(team):
    r = CodexRunner({})
    r._quota("gpt-6-luna", {"primary": {"usedPercent": 70, "resetsAt": int(LATER), "windowDurationMins": 300},
                            "secondary": {"usedPercent": 20}, "credits": {"hasCredits": True,
                                                                          "unlimited": False, "balance": "12.50"}})
    q = quota.for_model(team, "gpt-6-luna")
    assert q["window"] == "primary" and abs(q["remaining"] - 0.3) < 1e-6 and q["credits"] == "12.50"
    r._quota("gpt-6-luna", {"primary": {"usedPercent": 100}})
    assert quota.for_model(team, "gpt-6-luna")["status"] == "rejected"


def test_an_agent_on_a_low_model_is_marked_low(team):
    quota.note(team, "claude", "claude-opus-5-5", "five_hour", remaining=0.5, resets_at=LATER)
    quota.note(team, "claude", "claude-opus-5-5", "seven_day_opus", remaining=0.12, applies="opus")
    quota.note(team, "claude", "claude-sonnet-5-5", "seven_day_sonnet", remaining=0.9, applies="sonnet")
    models = Record(team).models()
    assert models["coder"]["quota"]["low"] is True and models["coder"]["quota"]["remaining"] == 0.12
    assert models["qa"]["quota"]["low"] is False
    doc = json.loads((team / "roster.json").read_text())
    doc["quota_low"] = 0.1
    (team / "roster.json").write_text(json.dumps(doc))
    assert Record(team).models()["coder"]["quota"]["low"] is False


def test_a_refused_limit_is_low_whatever_the_figure(team):
    quota.note(team, "claude", "claude-opus-5-5", "five_hour", status="rejected", resets_at=LATER)
    q = quota.for_model(team, "claude-opus-5-5")
    assert q["remaining"] == 0.0 and quota.low(q)


def test_nothing_known_is_nothing_shown(team):
    assert Record(team).models()["coder"]["quota"] == {}
    assert not (team / "quota.json").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
