"""Hosts that run their own tools, on the record like the rest.

Asked for by a team downstream: "Add a HostRunner hook (or post-tool entry
point) so non-ACP hosts emit slim tool_call events natively for trigger
learning, and unwrap generic MCP wrapper calls (ToolName / Arguments) in
triggers.due_for() just as policy.check_tool_policy() already does."

The Claude, ACP, Codex and SDK runners record each tool call as it
finishes. The host and agy runners do not see their agents' calls: the
host runs them. So nothing was learned from those turns, and Activity
showed no calls. The host's post-tool hook (`python -m ateam.hook_step
tool`) now records the call, in a supervised turn, where the runner does
not. And a call made through a host's one generic MCP-call tool is read as
the call it makes, by triggers and by what learns from them.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import hook_step, mcp_memory, memory_log, trigger_learn as learn, triggers  # noqa: E402
from ateam.runner import Runner  # noqa: E402
from ateam.runner_acp import AcpRunner  # noqa: E402
from ateam.runner_agy import AgyRunner  # noqa: E402
from ateam.runner_claude import ClaudeRunner  # noqa: E402
from ateam.runner_codex import CodexRunner  # noqa: E402
from ateam.runner_host import HostRunner  # noqa: E402
from ateam.supervisor import Supervisor  # noqa: E402

NO_RESOURCE = ('error: the server doesn\'t have a resource type "deployments" '
               'in namespace "default"')


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "teams" / "demo" / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": a, "role": "x"} for a in ("tpm", "coder")]}))
    (td / "conversations.json").write_text(json.dumps({"coder": "conv-1"}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(td.parent))
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "0")
    for var in ("ATEAM_AGENT", "ATEAM_WORKSPACE", "ATEAM_MEMORY_STORE", "ATEAM_MEMORY_CONFIG",
                "ATEAM_OBSERVER", "ATEAM_OBSERVER_CONFIG", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    triggers.forget_cache()
    yield td
    triggers.forget_cache()


def in_turn(team, agent="coder", **more):
    """The supervisor's ledger with a turn in flight, as a host runner's is."""
    (team / "inflight.json").write_text(json.dumps({agent: {
        "handle": {}, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "hop": 1, "trigger": {},
        "pull": False, "msgs": [], **more}}))


def events(team, kind="tool_call"):
    p = team / "events.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if r.get("event") == kind]


def post(team, **payload):
    """The host's hook after a tool call, with the conversation it ran in."""
    return hook_step.run("tool", {"conversation_id": "conv-1", **payload})


def save(team, name, content, **more):
    out = mcp_memory.handlers(memory_log.open_store(team, "coder"), team)["save_memory"](
        {"name": name, "description": "d", "content": content, **more})
    triggers.forget_cache()
    return out


# --- the hook puts the call on the record ----------------------------------------

def test_a_hosts_call_is_recorded_with_how_it_went(team):
    in_turn(team)
    assert post(team, tool_name="run_command", tool_input={"CommandLine": "kubectl get pods"},
                tool_response="web-1 Running", duration_ms=1500) == {}
    (ev,) = events(team)
    assert (ev["agent"], ev["tool"], ev["conversation"]) == ("coder", "run_command", "conv-1")
    assert ev["args"] == {"CommandLine": "kubectl get pods"}
    assert ev["result"] == "web-1 Running" and ev["error"] is None and ev["duration_s"] == 1.5


@pytest.mark.parametrize("payload, error", [
    ({"error": "permission denied"}, "permission denied"),
    ({"toolError": {"message": "no such file"}}, "no such file"),
    ({"tool_response": {"exitCode": 1, "stderr": NO_RESOURCE}}, "exit 1: " + NO_RESOURCE),
    ({"tool_response": {"error": "timed out after 30s"}}, "timed out after 30s"),
    ({"result": {"isError": True, "content": [{"type": "text", "text": "unknown channel #ops"}]}},
     "unknown channel #ops"),
])
def test_a_failure_is_read_from_whatever_the_host_sends(team, payload, error):
    in_turn(team)
    post(team, tool_name="run_command", tool_input={"CommandLine": "kubectl apply -f web.yaml"}, **payload)
    (ev,) = events(team)
    assert ev["error"] == error and ev["result"] is None


def test_results_come_in_several_shapes(team):
    in_turn(team)
    post(team, toolCall={"name": "view_file", "args": {"AbsolutePath": "/w/a.py"}, "output": "print(1)"})
    post(team, tool_name="send_to_teammate", tool_input={"to": "tpm"},
         tool_response={"content": [{"type": "text", "text": "[sent to tpm]"}]})
    post(team, tool_name="run_command", tool_input={"CommandLine": "true"},
         tool_response={"exitCode": 0, "stdout": "", "note": "ok"})
    post(team, tool_name="list_dir", tool_input={"path": "/w"})          # nothing on how it went
    a, b, c, d = events(team)
    assert (a["tool"], a["args"], a["result"]) == ("view_file", {"AbsolutePath": "/w/a.py"}, "print(1)")
    assert b["result"] == "[sent to tpm]" and b["error"] is None
    assert c["error"] is None and "exitCode" in c["result"]
    assert d["result"] is None and d["error"] is None and d["args"] == {"path": "/w"}


def test_a_long_result_is_kept_short(team):
    in_turn(team)
    post(team, tool_name="run_command", tool_input={"CommandLine": "cat big.log"},
         tool_response="x" * 50_000 + " END")
    (ev,) = events(team)
    assert len(ev["result"]) < 1_100 and ev["result"].endswith(" END")


def test_nothing_is_recorded_outside_a_supervised_turn(team):
    """The plugin's hooks run in a person's own sessions too."""
    post(team, tool_name="run_command", tool_input={"CommandLine": "ls"}, tool_response="a")
    assert events(team) == []
    in_turn(team, agent="tpm")                                  # someone else's turn
    post(team, tool_name="run_command", tool_input={"CommandLine": "ls"}, tool_response="a")
    assert events(team) == []
    assert hook_step.run("tool", {"conversation_id": "not-ours", "tool_name": "x"}) == {}
    assert events(team) == []


def test_a_call_the_runner_records_is_not_recorded_twice(team):
    in_turn(team, calls=True)
    post(team, tool_name="Bash", tool_input={"command": "ls"}, tool_response="a")
    assert events(team) == []


def test_a_hook_that_cannot_record_still_lets_the_turn_go_on(team, monkeypatch):
    in_turn(team)
    monkeypatch.setenv("ATEAM_OBSERVER", "nowhere:Nothing")
    assert post(team, tool_name="run_command", tool_input={"CommandLine": "ls"}) == {}


def test_the_hook_runs_as_the_host_runs_it(team):
    in_turn(team)
    env = {"PATH": "/usr/bin:/bin", "ATEAM_TEAM_DIR": str(team), "ATEAM_DURABLE_DIR": str(team.parent)}
    r = subprocess.run([sys.executable, "-m", "ateam.hook_step", "tool"], cwd=ROOT, env=env,
                       input=json.dumps({"conversation_id": "conv-1", "tool_name": "run_command",
                                         "tool_input": {"CommandLine": "git push"},
                                         "tool_response": {"exitCode": 1, "stderr": "rejected: protected branch"}}),
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0 and json.loads(r.stdout) == {}, r.stderr
    (ev,) = events(team)
    assert ev["error"] == "exit 1: rejected: protected branch"


# --- which runners record their own ---------------------------------------------

def test_each_runner_says_whether_it_records_calls():
    assert all(r.records_calls for r in (ClaudeRunner, AcpRunner, CodexRunner))
    assert not any(r.records_calls for r in (HostRunner, AgyRunner, Runner))
    sdk = (ROOT / "ateam/runner_sdk.py").read_text()
    assert 'label = "sdk"\n    records_calls = True' in sdk
    for name in ("runner_claude", "runner_acp", "runner_codex", "runner_sdk"):
        assert "record_tool_call(" in (ROOT / "ateam" / f"{name}.py").read_text(), name
    for name in ("runner_host", "runner_agy"):
        assert "record_tool_call(" not in (ROOT / "ateam" / f"{name}.py").read_text(), name


class Stub(Runner):
    label = "stub"

    def wake(self, agent, message):
        return "ok"


def test_the_turns_ledger_says_who_records(team):
    class Recording(Stub):
        records_calls = True

    class PerAgent(Stub):
        def records_calls_for(self, agent):
            return agent == "tpm"
    turn = {"handle": {}, "ts": "2026-11-12 10:00:00", "hop": 1, "trigger": {}, "msgs": []}
    for runner, want in ((Stub(), {"coder": False, "tpm": False}),
                         (Recording(), {"coder": True, "tpm": True}),
                         (PerAgent(), {"coder": False, "tpm": True})):
        sup = Supervisor(["coder", "tpm"], runner, require_review=False, quiet=True)
        sup._owns_ledger = True
        sup.inflight = {"coder": dict(turn), "tpm": dict(turn)}
        sup._save_ledger()
        doc = json.loads((team / "inflight.json").read_text())
        assert {a: doc[a]["calls"] for a in doc} == want, type(runner).__name__
        sup.inflight = {}


# --- a call inside a host's generic MCP call ------------------------------------

WRAPPED = {"ServerName": "ateam_bus", "ToolName": "send_to_teammate",
           "Arguments": {"to": "user", "content": "done, shipping"}}


def test_the_call_inside_a_generic_call_is_the_call():
    assert triggers.unwrap("call_mcp_tool", WRAPPED) == ("send_to_teammate", WRAPPED["Arguments"])
    assert triggers.unwrap("call_mcp_tool", {"ToolName": "save_memory",
                                             "Arguments": '{"name": "x"}'}) == ("save_memory", {"name": "x"})
    assert triggers.unwrap("call_mcp_tool", {"toolName": "memory_index"}) == ("memory_index", {})
    plain = {"command": "ls"}
    assert triggers.unwrap("Bash", plain) == ("Bash", plain)
    assert triggers.unwrap("Bash", None) == ("Bash", {})


def test_a_trigger_sees_through_the_hosts_generic_call(team):
    save(team, "before-telling-the-user", "Say what was verified and what was not.",
         triggers="send_to_teammate(user)")
    assert [h["via"] for h in triggers.due_for(team, "coder", "call_mcp_tool", WRAPPED)] \
        == ["send_to_teammate(user)"]
    to_tpm = {**WRAPPED, "Arguments": {"to": "tpm", "content": "fyi"}}
    assert triggers.due_for(team, "coder", "call_mcp_tool", to_tpm) == []
    held = triggers.hold(team, "coder", "call_mcp_tool", WRAPPED)
    assert held.startswith("[held: this send_to_teammate call did not run") and "what was verified" in held
    (ev,) = events(team, "triggered")
    assert ev["tool"] == "send_to_teammate" and ev["family"] == "send_to_teammate(user)"


def test_a_trigger_on_the_generic_call_itself_still_matches(team):
    save(team, "mcp-calls", "Check the server is the team's before calling it.",
         triggers="call_mcp_tool")
    assert [h["via"] for h in triggers.due_for(team, "coder", "call_mcp_tool", WRAPPED)] == ["call_mcp_tool"]


def test_the_pre_tool_hook_holds_a_wrapped_call(team):
    from ateam import hook_pre_tool_use
    save(team, "before-telling-the-user", "Say what was verified and what was not.",
         triggers="send_to_teammate(user)")
    code, why = hook_pre_tool_use.decide({"conversation_id": "conv-1", "tool_name": "call_mcp_tool",
                                          "tool_input": WRAPPED}, team)
    assert code == 2 and "what was verified" in why


# --- and so a host's turns are learned from ---------------------------------------

def test_a_trigger_is_learned_from_a_hosts_turn(team):
    """The whole of it: the host's hook records a call that fails, the agent
    opens a memory through the host's generic MCP call, the call made again
    works, and that memory comes up before the next such call."""
    save(team, "kubectl-staging", "Always pass --context staging; apply needs -n web.")
    in_turn(team)
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    post(team, tool_name="run_command", tool_input={"CommandLine": "kubectl apply -f web.yaml"},
         tool_response={"exitCode": 1, "stderr": NO_RESOURCE})
    post(team, tool_name="call_mcp_tool",
         tool_input={"ServerName": "ateam_memory", "ToolName": "read_memory",
                     "Arguments": {"name": "kubectl-staging"}},
         tool_response={"content": [{"type": "text", "text": "Always pass --context staging"}]})
    post(team, tool_name="run_command",
         tool_input={"CommandLine": "kubectl apply -f web.yaml -n web --context staging"},
         tool_response={"exitCode": 0, "stdout": "deployment.apps/web configured"})
    lines = learn.observe_turn(team, "coder", since)
    triggers.forget_cache()
    assert any("kubectl-staging" in l for l in lines), lines
    learned, _ = learn.standing(team, "coder")
    assert learned.get("kubectl-staging") == ["Bash(kubectl apply)"]
    again = triggers.hold(team, "coder", "run_command", {"CommandLine": "kubectl apply -f api.yaml"})
    assert "--context staging" in again


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
