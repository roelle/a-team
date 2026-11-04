"""The ACP runner, against a scripted ACP agent (evals/fake_acp_agent.py).

Asked for: agents on any harness that speaks ACP, starting with OpenCode, so
a team can be collected from many agent platforms. These pin what the
runner owes the team on any of them: the session survives between turns,
the agent gets its brief and the team's MCP servers, its model is set, a
permission request is answered from the team's policy, and what it did is
on the record. A live OpenCode turn was run by hand (see the CHANGELOG).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import quota, runner as runner_lib  # noqa: E402
from ateam.runner_acp import AcpRunner, team_tool  # noqa: E402

FAKE = ROOT / "evals" / "fake_acp_agent.py"


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (tmp_path / "answers").mkdir()
    (td / "roster.json").write_text(json.dumps({
        "policy": {"refuse_paths": [str(tmp_path / "answers")]},
        "agents": [{"name": "coder", "role": "builds"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("FAKE_ACP_STATE", str(tmp_path / "fake.json"))
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "0")
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.setattr(runner_lib, "RETRY_BASE_SECONDS", 0.0)
    return td


def runner(**config):
    return AcpRunner({"command": [sys.executable, str(FAKE)], **config})


def fake(team):
    return json.loads((team.parent / "fake.json").read_text())


def prompts(team):
    return ["".join(b["text"] for b in e["params"]["prompt"])
            for e in fake(team)["log"] if e["method"] == "session/prompt"]


def events(team, kind):
    return [json.loads(l) for l in (team / "events.jsonl").read_text().splitlines()
            if json.loads(l).get("event") == kind]


def test_the_session_survives_between_turns_and_the_brief_comes_once(team):
    r = runner()
    assert r.wake("coder", "first").endswith("first")
    assert r.conversation_id("coder") == "ses_1"
    r.wake("coder", "second")
    first, second = prompts(team)
    assert "coder" in first and first.endswith("first"), "a new session starts with the brief"
    assert second == "second", "a resumed one does not get it again"
    methods = [e["method"] for e in fake(team)["log"]]
    assert methods.count("session/new") == 1 and methods.count("session/resume") == 1
    assert fake(team)["sessions"]["ses_1"]["mcp"] == ["ateam_memory", "ateam_bus",
                                                      "ateam_tasks", "ateam_self"]


def test_a_session_the_harness_lost_is_started_again(team):
    r = runner()
    r.wake("coder", "first")
    st = fake(team)
    st["sessions"] = {}
    (team.parent / "fake.json").write_text(json.dumps(st))
    assert "heard" in r.wake("coder", "again")
    assert r.conversation_id("coder") == "ses_1" and len(fake(team)["sessions"]) == 1


def test_the_rosters_model_is_set_on_the_session(team):
    doc = json.loads((team / "roster.json").read_text())
    doc["agents"][0]["model"] = "small"
    (team / "roster.json").write_text(json.dumps(doc))
    assert runner().wake("coder", "go").startswith("[small]")
    doc["agents"][0]["model"] = "nonesuch"
    (team / "roster.json").write_text(json.dumps(doc))
    out = runner().wake("coder", "go")
    assert out.startswith("[error:") and "does not offer the model 'nonesuch'" in out


def test_a_permission_request_is_answered_from_the_teams_policy(team):
    r = runner()
    assert r.wake("coder", "run: ls -la") == "permission: once"
    answers = team.parent / "answers"
    assert r.wake("coder", f"run: cat {answers}/key.txt") == "permission: reject"
    calls = events(team, "tool_call")
    assert [c.get("error") is None for c in calls] == [True, False]


def test_what_the_agent_did_is_on_the_record(team):
    r = runner()
    r.wake("coder", "team-tool")
    call = events(team, "tool_call")[-1]
    assert call["tool"] == "send_to_teammate" and call["args"] == {"to": "qa", "content": "hi"}
    turns = events(team, "turn")
    assert turns[-1]["input_tokens"] == 100 and turns[-1]["context_tokens"] == 1000
    assert turns[-1]["cost_usd"] == 0.01
    r.wake("coder", "again")
    assert events(team, "turn")[-1]["cost_usd"] == 0.01, "the running total, as each turn's share"


def test_a_full_model_falls_back(team):
    doc = json.loads((team / "roster.json").read_text())
    doc["fallback_models"] = ["medium"]
    (team / "roster.json").write_text(json.dumps(doc))
    out = runner(model="big").wake("coder", "fail429")
    assert out.startswith("[medium]"), out
    assert quota.cooling(team, "big")


def test_the_models_are_the_harnesss_own(team):
    got = AcpRunner.list_models({"command": [sys.executable, str(FAKE)]})
    assert [m["id"] for m in got] == ["big", "small", "medium"]


def test_the_check_a_plugin_runs(team):
    answers = team.parent / "answers"
    run = lambda doc: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "ateam.runner_acp", "check"], input=json.dumps(doc),
        capture_output=True, text=True, cwd=ROOT)
    no = run({"agent": "coder", "tool": "read", "args": {"filePath": f"{answers}/key.txt"}})
    assert no.returncode == 2 and "refuse_paths" in no.stdout
    yes = run({"agent": "coder", "tool": "read", "args": {"filePath": str(ROOT / "README.md")}})
    assert yes.returncode == 0


def test_team_tools_are_named_as_the_policy_knows_them():
    assert team_tool("ateam_bus_send_to_teammate") == "send_to_teammate"
    assert team_tool("ateam_tasks_create_task") == "create_task"
    assert team_tool("bash") == "bash"


def test_a_harness_that_is_not_installed_says_so(team):
    out = AcpRunner({"command": ["no-such-harness-here", "acp"]}).wake("coder", "go")
    assert out.startswith("[error:") and "not installed" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
