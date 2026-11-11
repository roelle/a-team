"""Memories and norms that come up before the call they are about.

Asked for: "For a lot of tool use, there are memories associated with the
tool: gotchas, etc. I'm seeing those get loaded in every wake. Could we
assign memories to tools, and always, and only, load them before using the
tool?" Then: "I think first use is held for memories about it", "memories
or rules for tasks, wiki too", and "make sure the rules get in after any
compaction and don't get diluted for long-lived agents."

These pin that a memory names its triggers when it is saved, keeps them,
and is no longer recalled on a wake; that the first matching call is held
with what the agent has on it, and the second goes through; that a section
of NORMS.md can name triggers too; that what was shown is shown again after
a new conversation, a compaction, enough new context or enough time; and
that every place a call can be held holds it, and the rest get a note after.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import hook_pre_tool_use, mcp_base, mcp_memory, memory_log, steer, triggers  # noqa: E402
from ateam.recall import AgentRecall  # noqa: E402

AGENTS = ["tpm", "coder"]
KUBECTL = ("Always pass --context staging; the default context is production. "
           "`kubectl apply` needs -n web or it lands in default.")


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "teams" / "demo" / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": a, "role": "x"} for a in AGENTS]}))
    (td / "conversations.json").write_text(json.dumps({"coder": "conv-1"}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(td.parent))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    for var in ("ATEAM_WORKSPACE", "ATEAM_MEMORY_STORE", "ATEAM_MEMORY_CONFIG", "ATEAM_OBSERVER"):
        monkeypatch.delenv(var, raising=False)
    triggers.forget_cache()
    yield td
    triggers.forget_cache()


def tools(team, agent="coder"):
    return mcp_memory.handlers(memory_log.open_store(team, agent), team)


def remember_kubectl(team):
    out = tools(team)["save_memory"]({
        "name": "kubectl-gotchas", "description": "Before any kubectl command",
        "content": KUBECTL, "why": "applied to production twice",
        "triggers": "Bash(kubectl *); Bash(helm *)"})
    triggers.forget_cache()
    return out


def kubectl(team, cmd="kubectl apply -f web.yaml", tool="Bash"):
    triggers.forget_cache()
    return triggers.hold(team, "coder", tool, {"command": cmd})


def turn(team, agent, context):
    with (team / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": "2026-11-11 09:00:00", "event": "turn", "agent": agent,
                            "conversation": "conv-1", "context_tokens": context}) + "\n")


# --- a memory names its triggers ----------------------------------------------------

def test_a_memory_is_saved_with_its_triggers_and_keeps_them(team):
    out = remember_kubectl(team)
    assert "comes up before Bash(kubectl *); Bash(helm *)" in out
    t = tools(team)
    assert "[kubectl-gotchas] (before Bash(kubectl *); Bash(helm *)) Before any" in t["memory_index"]({})
    raw = t["read_memory"]({"name": "kubectl-gotchas"})
    assert "- Triggers: Bash(kubectl *); Bash(helm *)" in raw and "- Why: applied" in raw
    t["save_memory"]({"name": "kubectl-gotchas", "description": "d", "content": raw + "\nAlso: -o yaml"})
    raw = t["read_memory"]({"name": "kubectl-gotchas"})
    assert triggers.of(raw) == ["Bash(kubectl *)", "Bash(helm *)"], "a re-save keeps them"
    assert raw.count("Triggers:") == 1 and "- Why: applied" in raw
    t["save_memory"]({"name": "kubectl-gotchas", "description": "d", "content": "x", "triggers": "none"})
    assert triggers.of(t["read_memory"]({"name": "kubectl-gotchas"})) == []


def test_a_trigger_that_is_not_a_call_is_refused(team):
    out = tools(team)["save_memory"]({"name": "m", "description": "d", "content": "c",
                                      "triggers": "before deploying"})
    assert out.startswith("[error:") and "Bash(kubectl *)" in out


def test_a_merge_keeps_the_triggers_of_what_it_merged(team):
    t = tools(team)
    t["save_memory"]({"name": "a", "description": "d", "content": "one", "triggers": "Bash(kubectl *)"})
    t["save_memory"]({"name": "b", "description": "d", "content": "two", "triggers": "Bash(helm *)"})
    t["merge_memories"]({"names": "a,b", "into": "k8s", "description": "d", "content": "one; two"})
    assert triggers.of(t["read_memory"]({"name": "k8s"})) == ["Bash(kubectl *)", "Bash(helm *)"]


def test_a_memory_with_triggers_is_not_recalled_on_a_wake(team):
    remember_kubectl(team)
    tools(team)["save_memory"]({"name": "user-prefs", "description": "d", "content": "short replies"})
    names = [m["name"] for m in AgentRecall(team, "coder", {}).memories()]
    assert names == ["user-prefs"]


# --- the first matching call is held ------------------------------------------------

def test_the_first_matching_call_is_held_and_the_second_goes_through(team):
    remember_kubectl(team)
    held = kubectl(team, "cd deploy && kubectl apply -f web.yaml")
    assert held.startswith("[held: this Bash call did not run") and KUBECTL in held
    assert "applied to production" not in held, "the lesson, not its provenance"
    assert kubectl(team) == ""
    assert kubectl(team, "ls -la") == "" and kubectl(team, "helm list") == "", \
        "one showing covers every trigger of the memory"
    shown = [json.loads(l) for l in (team / "events.jsonl").read_text().splitlines()]
    assert shown[-1]["event"] == "triggered" and shown[-1]["shown"] == ["memory:kubectl-gotchas"]


def test_every_hosts_shell_and_every_hosts_team_tool_names_match():
    assert triggers.matches("Bash(git push)", "run_command", {"CommandLine": "git push origin"})
    assert triggers.matches("Bash(git push)", "exec_command", {"cmd": ["git", "push"]})
    assert not triggers.matches("Bash(git push)", "Bash", {"command": "git pushd"})
    for name in ("send_to_teammate", "mcp__ateam_bus__send_to_teammate", "ateam_bus_send_to_teammate"):
        assert triggers.matches("send_to_teammate(user)", name, {"to": "user", "content": "x"})
    assert not triggers.matches("send_to_teammate(user)", "send_to_teammate", {"to": "qa"})
    assert triggers.matches("Edit(*.ipynb)", "Edit", {"file_path": "/w/a.ipynb"})
    assert triggers.matches("mcp__github__*", "mcp__github__create_issue", {})


def test_a_task_trigger_matches_the_tasks_project(team):
    from ateam import tasks
    t = tasks.create(team, "web", "fix the form", created_by="user")
    assert triggers.matches("create_task(web*)", "create_task", {"project": "web-app", "title": "x"})
    assert triggers.matches("claim_task(web)", "claim_task", {"task_id": t["id"]}, team)
    assert not triggers.matches("claim_task(home)", "claim_task", {"task_id": t["id"]}, team)


def test_a_team_norm_can_name_triggers_too(team):
    (team / "NORMS.md").write_text(
        "# Norms\n\nBe kind.\n\n## Writing to the user\nTriggers: send_to_teammate(user)\n\n"
        "Lead with the decision you need from them.\n\n## Other\nNo triggers here.\n")
    for agent in AGENTS:
        held = triggers.hold(team, agent, "send_to_teammate", {"to": "user", "content": "hi"})
        assert "Team norm: Writing to the user" in held and "Lead with the decision" in held
        assert "No triggers here" not in held and "Be kind" not in held
    assert triggers.hold(team, "coder", "send_to_teammate", {"to": "tpm"}) == ""


def test_it_can_be_turned_off(team):
    remember_kubectl(team)
    doc = json.loads((team / "roster.json").read_text())
    doc["memory_triggers"] = False
    (team / "roster.json").write_text(json.dumps(doc))
    assert kubectl(team) == ""


# --- shown again when it may be gone ---------------------------------------------------

def test_a_new_conversation_shows_it_again(team):
    remember_kubectl(team)
    assert kubectl(team) and not kubectl(team)
    (team / "conversations.json").write_text(json.dumps({"coder": "conv-2"}))
    assert kubectl(team)


def test_a_compaction_shows_it_again_whoever_notices(team):
    remember_kubectl(team)
    turn(team, "coder", 150_000)
    assert kubectl(team) and not kubectl(team)
    turn(team, "coder", 40_000)                 # the host compacted, and said nothing
    assert kubectl(team), "a context half the size has lost it"
    assert not kubectl(team)
    triggers.rearm(team, "coder", "compacted")  # a runner that saw it
    assert kubectl(team)


def test_a_long_lived_agent_is_shown_it_again_as_it_fades(team):
    remember_kubectl(team)
    turn(team, "coder", 20_000)
    assert kubectl(team)
    turn(team, "coder", 90_000)
    assert not kubectl(team)
    turn(team, "coder", 130_000)                # 110k of new context since
    assert kubectl(team)
    later = time.time() + 7 * 3600
    assert triggers.check(team, "coder", "Bash", {"command": "kubectl get pods"}, now=later)


def test_a_changed_memory_is_shown_again(team):
    remember_kubectl(team)
    assert kubectl(team) and not kubectl(team)
    tools(team)["save_memory"]({"name": "kubectl-gotchas", "description": "d",
                                "content": KUBECTL + " Also: never `delete ns`."})
    assert "never `delete ns`" in kubectl(team)


# --- where a call is held, and where it is noted after ----------------------------------

def test_the_generic_hook_holds_it(team, monkeypatch):
    remember_kubectl(team)
    monkeypatch.setenv("ATEAM_AGENT", "coder")
    code, text = hook_pre_tool_use.decide({"tool_name": "Bash",
                                           "tool_input": {"command": "kubectl get pods"}}, team)
    assert code == 2 and KUBECTL in text
    triggers.forget_cache()
    assert hook_pre_tool_use.decide({"tool_name": "Bash",
                                     "tool_input": {"command": "kubectl get pods"}}, team) == (0, "")


def test_a_team_tool_holds_it_on_any_host(team, monkeypatch):
    (team / "NORMS.md").write_text("## To the user\nTriggers: send_to_teammate(user)\n\nOne line first.\n")
    monkeypatch.setenv("ATEAM_AGENT", "coder")
    assert "One line first" in mcp_base._held("send_to_teammate", {"to": "user", "content": "x"})
    assert mcp_base._held("send_to_teammate", {"to": "user", "content": "x"}) == ""


def test_where_nothing_could_hold_it_the_next_step_says_it(team):
    remember_kubectl(team)
    (team / "inflight.json").write_text(json.dumps({"coder": {"ts": "2026-11-11 09:00:00", "msgs": []}}))
    steer.count_call(team, "coder", "run_command", {"CommandLine": "kubectl apply -f x"})
    note = steer.take_notes(team, "coder")
    assert note.startswith("[Your last run_command call ran") and KUBECTL in note
    steer.count_call(team, "coder", "run_command", {"CommandLine": "kubectl apply -f x"})
    assert steer.take_notes(team, "coder") == ""


def test_every_runner_that_can_hold_a_call_does():
    claude = (ROOT / "ateam/runner_claude.py").read_text()
    assert "triggers.hold(team_dir, agent, raw, args)" in claude
    assert 'm.subtype == "compact_boundary"' in claude and "triggers.rearm(" in claude
    assert "triggers.hold(_team_dir, name, tname" in (ROOT / "ateam/sdk_agent.py").read_text()
    assert "triggers.hold(os.environ.get(\"ATEAM_TEAM_DIR\")" in (ROOT / "ateam/runner_acp.py").read_text()
    codex = (ROOT / "ateam/runner_codex.py").read_text()
    assert '"compact" in method.lower()' in codex and "triggers.rearm(" in codex


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
