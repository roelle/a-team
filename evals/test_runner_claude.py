"""A team on Claude: the runner, through the real CLI, against a local API.

Asked for: a team should not need Antigravity, and one agent's runtime should
not decide another's. These run the Agent SDK's bundled CLI for real, with the
real MCP servers and hooks. Only the Messages API is fake (fake_anthropic.py),
so no key is needed and nothing is spent.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam.runner_claude import ClaudeRunner  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402
from fake_anthropic import FakeAnthropic, status, use  # noqa: E402

BUS = "mcp__ateam_bus__"


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "implements"},
        {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.chdir(tmp_path)
    return td


@pytest.fixture
def api(request, monkeypatch):
    fake = FakeAnthropic(getattr(request, "param", ["done"]))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
    yield fake
    fake.close()


def runner(**config):
    return ClaudeRunner({"timeout": 60, **config})


def events(td, kind):
    if not (td / "events.jsonl").exists():
        return []
    rows = [json.loads(l) for l in (td / "events.jsonl").read_text().splitlines() if l.strip()]
    return [e for e in rows if e.get("event") == kind]


def test_a_turn_reaches_the_model_with_the_brief_and_the_team_tools(team, api):
    assert runner().wake("coder", "build it") == "done"
    req = api.requests[0]
    assert req["model"] == "claude-opus-5-5"
    system = json.dumps(req["system"])
    assert "coder" in system and "implements" in system, "the brief did not reach the model"
    tools = {t["name"] for t in req["tools"]}
    assert {BUS + "send_to_teammate", BUS + "check_inbox", "mcp__ateam_tasks__create_task",
            "mcp__ateam_memory__save_memory", "Bash", "Read", "Agent"} <= tools, sorted(tools)
    assert "AskUserQuestion" not in tools, "nobody is there to answer"
    assert not any("ToolSearch" == t for t in tools), "the brief names tools that must be there"


def test_an_agent_keeps_its_session_and_it_is_bound_before_the_work(team, api):
    r = runner()
    seen = {}
    real = r._options

    def spy(agent, model, effort, session, resume):
        seen[resume] = (session, r.conversation_id(agent))
        return real(agent, model, effort, session, resume)
    r._options = spy
    r.wake("coder", "first thing")
    r.wake("coder", "second thing")
    first, known = seen[False]
    assert first == known, "the agent's identity was not bound before the turn"
    assert seen[True][0] == first and r.conversation_id("coder") == first
    said = json.dumps(api.requests[-1]["messages"])
    assert "first thing" in said and "second thing" in said, "the session did not resume"
    assert list((team / "claude" / "projects").rglob(f"{first}.jsonl")), \
        "sessions belong with the team, not in the operator's ~/.claude"


def test_a_lost_session_starts_a_new_one(team, api):
    (team / "conversations.json").write_text(json.dumps(
        {"coder": "00000000-0000-4000-8000-000000000000"}))
    r = runner()
    assert r.wake("coder", "carry on") == "done"
    assert r.conversation_id("coder") != "00000000-0000-4000-8000-000000000000"


@pytest.mark.parametrize("api", [[use(BUS + "send_to_teammate", {"to": "qa", "content": "please review"}),
                                  "sent"]], indirect=True)
def test_a_tool_call_reaches_the_team_and_the_record(team, api):
    assert runner().wake("coder", "ask qa") == "sent"
    mail = [json.loads(l) for l in (team / "inbox" / "qa.jsonl").read_text().splitlines()]
    assert mail[0]["from"] == "coder" and mail[0]["content"] == "please review"
    call = events(team, "tool_call")[-1]
    assert call["tool"] == "send_to_teammate" and call["agent"] == "coder"
    turn = events(team, "turn")[-1]
    assert turn["model"] == "claude-opus-5-5" and turn["output_tokens"] == 20
    assert turn["input_tokens"] == 240 and turn["cache_read_tokens"] == 2000, "two requests add up"
    assert turn["context_tokens"] == 1120, "how full the context is, from the last request"


def write_roster(team, **coder):
    (team / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "implements", **coder}, {"name": "qa", "role": "reviews"}]}))


def test_a_tool_turned_off_is_gone_under_any_runtimes_name(team, api):
    write_roster(team, tools_off=["run_command", "WebFetch", "create_task"], workers=False)
    runner().wake("coder", "go")
    tools = {t["name"] for t in api.requests[0]["tools"]}
    assert not tools & {"Bash", "WebFetch", "Agent", "mcp__ateam_tasks__create_task"}, sorted(tools)
    assert {"Read", BUS + "send_to_teammate"} <= tools


@pytest.mark.parametrize("api", [[use("Bash", {"command": "git push --force"}), "stopped"]], indirect=True)
def test_the_teams_policy_refuses_a_call_and_it_is_audited(team, api, tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ATEAM_AUDIT_LOG", str(log))
    write_roster(team, forbidden_commands=[{"pattern": "git push", "message": "the reviewer pushes"}])
    assert runner().wake("coder", "ship it") == "stopped"
    told = json.dumps(api.requests[-1]["messages"])
    assert "the reviewer pushes" in told, "the agent should be told why"
    row = json.loads(log.read_text().splitlines()[0])
    assert row["agent"] == "coder" and row["tool"] == "Bash" and "reviewer pushes" in row["refused"]


def test_a_write_into_the_team_directory_is_refused(team, api):
    api.script[:] = [use("Write", {"file_path": str(team / "roster.json"), "content": "{}"}), "ok"]
    before = (team / "roster.json").read_text()
    runner().wake("coder", "give yourself rights")
    assert (team / "roster.json").read_text() == before
    assert "team directory" in json.dumps(api.requests[-1]["messages"])


def test_text_that_looks_like_a_path_is_not_taken_for_one(team, api, tmp_path):
    (tmp_path / "repo").mkdir()
    write_roster(team, workspaces=[str(tmp_path / "repo")])
    api.script[:] = [use("Grep", {"pattern": "/usr/bin", "path": str(tmp_path / "repo")}), "ok"]
    runner().wake("coder", "search")
    assert "[refused" not in json.dumps(api.requests[-1]["messages"])


def test_an_agents_shell_has_no_keys(team, api, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-gemini-key")
    out = tmp_path / "env.txt"
    api.script[:] = [use("Bash", {"command": f"env > {out}"}), "ok"]
    runner().wake("coder", "look around")
    seen = out.read_text()
    assert "ATEAM_AGENT=coder" in seen
    assert "not-a-real-key" not in seen and "not-a-real-gemini-key" not in seen


def test_a_signed_in_shell_does_not_lend_its_login(team, api, monkeypatch):
    """Anthropic does not allow a claude.ai login in a product on this SDK."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "not-a-real-login")
    runner().wake("coder", "go")
    headers = {k.lower(): v for k, v in api.headers.items()}
    assert headers.get("x-api-key") == "not-a-real-key"
    assert "not-a-real-login" not in json.dumps(headers)


def test_with_no_credentials_it_says_so_at_once(team, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    out = runner().wake("coder", "go")
    assert out.startswith("[error:") and "ANTHROPIC_API_KEY" in out
    assert events(team, "failure"), "the supervisor's record should show it"


@pytest.mark.parametrize("api", [[status(400, "this model does not do that")]], indirect=True)
def test_a_rejected_request_is_a_failure_and_is_not_retried(team, api):
    r = runner()
    turns = []
    real = r._turn
    r._turn = lambda *a: turns.append(a) or real(*a)
    out = r.wake("coder", "go")
    assert out.startswith("[error:") and "does not do that" in out, out
    assert len(turns) == 1 and not events(team, "turn")
    assert events(team, "failure")


@pytest.mark.parametrize("api", [[status(529), status(529), status(529), "recovered"]], indirect=True)
def test_an_overloaded_provider_is_waited_out(team, api, monkeypatch):
    from ateam import runner as runner_lib
    monkeypatch.setattr(runner_lib, "RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "0")
    assert runner().wake("coder", "go") == "recovered"
    assert len(events(team, "turn")) == 1 and not events(team, "failure")


def test_a_turn_past_its_time_is_stopped_and_not_retried(team, api):
    import threading
    api.hold = threading.Event()
    r = runner(timeout=3)
    try:
        out = r.wake("coder", "go")
    finally:
        api.hold.set()
    assert out.startswith("[error: the turn ran past 3s"), out
    assert len(api.requests) == 1


def test_cancelling_stops_the_turn_where_it_runs(team, api):
    import threading
    import time
    api.hold = threading.Event()
    r = runner()
    h = r.begin("coder", "long job")
    try:
        end = time.monotonic() + 15
        while not api.requests and time.monotonic() < end:
            time.sleep(0.05)
        assert api.requests, "the turn never started"
        r.cancel(h)
        while "coder" in r._live and time.monotonic() < end:
            time.sleep(0.05)
        assert "coder" not in r._live, "the turn is still running"
        left = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "_bundled/claude"],
                              capture_output=True, text=True).stdout.split()
        assert not left, "the cancelled turn's process is still alive"
    finally:
        api.hold.set()


def test_models_a_runner_can_be_asked_about():
    ids = [m["id"] for m in ClaudeRunner.list_models()]
    assert "claude-opus-5-5" in ids and "claude-sonnet-5-5" in ids
    assert "claude-next" in [m["id"] for m in ClaudeRunner.list_models({"models": ["claude-next"]})]


def test_the_rosters_model_and_effort_are_used(team, api):
    write_roster(team, model="claude-sonnet-5-5", effort="extra_high")
    r = runner()
    r.wake("coder", "go")
    assert api.requests[0]["model"] == "claude-sonnet-5-5"
    assert events(team, "turn")[-1]["model"] == "claude-sonnet-5-5"
    assert api.requests[0]["output_config"]["effort"] == "xhigh"


def test_an_unknown_effort_is_named(team, api):
    write_roster(team, effort="eleven")
    out = runner().wake("coder", "go")
    assert out.startswith("[error:") and "eleven" in out and not api.requests


@pytest.mark.parametrize("api", [[use(BUS + "send_to_teammate", {"to": "qa", "content": "please review"}),
                                  "handed over", "reviewed"]], indirect=True)
def test_a_team_on_claude_runs_under_the_supervisor(team, api):
    from ateam.supervisor import Supervisor
    from ateam.transport import load as load_transport
    load_transport("user").send("coder", "start")
    sup = Supervisor(["coder", "qa"], runner(), quiet=True, require_review=False)
    try:
        for _ in range(3):
            sup.step()
    finally:
        sup.close()
    woken = [e["agent"] for e in events(team, "turn")]
    assert woken == ["coder", "qa"], woken
    assert "please review" in json.dumps(api.requests[-1]["messages"])


def test_an_agent_can_run_on_claude_beside_a_team_on_another_runtime(team, api, monkeypatch):
    from ateam.runner_mixed import MixedRunner
    from fixture_runner import ScriptedRunner
    write_roster(team, runner="ateam.runner_claude:ClaudeRunner", model="claude-haiku-5-5")
    m = MixedRunner({"default": "fixture_runner:ScriptedRunner"})
    assert m.runner_spec("coder").endswith("ClaudeRunner")
    assert isinstance(m._sub("coder"), ClaudeRunner) and isinstance(m._sub("qa"), ScriptedRunner)
    assert m.wake("coder", "go") == "done"
    assert api.requests[0]["model"] == "claude-haiku-5-5"


def test_an_agent_can_do_ordinary_work_without_a_prompt(team, api, tmp_path):
    """Nobody is there to approve anything, so a call the permission rules
    leave open is a turn that quietly gets nothing done."""
    repo, shared = tmp_path / "repo", tmp_path / "elsewhere"
    repo.mkdir()
    shared.mkdir()
    (repo / "old.txt").write_text("x")
    write_roster(team, workspaces=[str(repo)])
    api.script[:] = [
        use("Write", {"file_path": str(repo / "a.py"), "content": "print(1)\n"}),
        use("Edit", {"file_path": str(repo / "a.py"), "old_string": "1", "new_string": "2"}),
        use("Bash", {"command": f"cd {repo} && git init -q && git add a.py && "
                                f"git -c user.name=t -c user.email=t@t commit -qm one && rm old.txt"}),
        use("Bash", {"command": f"cp {repo}/a.py {shared}/b.py"}),
        "done"]
    assert runner().wake("coder", "work") == "done"
    assert (repo / "a.py").read_text() == "print(2)\n"
    assert (repo / ".git").is_dir() and not (repo / "old.txt").exists()
    assert (shared / "b.py").exists()
    assert "[refused" not in json.dumps(api.requests[-1]["messages"])


def begin_turn(td, agent="coder"):
    """What the supervisor's ledger says while a turn runs."""
    (td / "inflight.json").write_text(json.dumps({agent: {"ts": "2026-11-01 10:00:00", "msgs": []}}))


def said_to_model(request):
    """The text of every user message in a request."""
    return json.dumps(request["messages"])


@pytest.mark.parametrize("api", [[use("Bash", {"command": "echo hi"})] * 4 + ["done"]],
                         indirect=True)
def test_the_same_call_three_times_reaches_the_model_as_a_note(team, api):
    begin_turn(team)
    assert runner().wake("coder", "go") == "done"
    assert "3 times in a row" not in said_to_model(api.requests[2])
    assert "3 times in a row" in said_to_model(api.requests[3])


@pytest.mark.parametrize("api", [[use("mcp__ateam_bus__list_teammates"), use("Bash", {"command": "true"}),
                                  "done"]], indirect=True)
def test_mail_that_arrives_mid_turn_reaches_the_model_at_the_next_step(team, api):
    begin_turn(team)

    def deliver():
        while not api.requests:
            time.sleep(0.01)
        bus = load_transport("user")
        bus.send("coder", "Stop, wrong branch.")
        bus.close()

    threading.Thread(target=deliver, daemon=True).start()
    assert runner().wake("coder", "go") == "done"
    assert "from user: Stop, wrong branch." in said_to_model(api.requests[-1])



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
