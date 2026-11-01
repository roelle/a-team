"""What reaches an agent between the steps of a turn.

Asked for after reading how another harness steers its agents: here, a message
to a busy agent waited for its turn to end unless it thought to call
check_inbox, and an agent stuck on one call had nobody to tell it. The runtimes
differ in how a note arrives, so what is tested at this level is what they all
share: what is due, and that it is said once. The Claude path is tested against
the real CLI in test_runner_claude.
"""
import asyncio
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import hook_step, mcp_base, mcp_bus, receipts, steer  # noqa: E402
from ateam.supervisor import Supervisor  # noqa: E402
from ateam.transport import Message  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402
from fixture_runner import ScriptedRunner  # noqa: E402


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "builds"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    return td


def begin_turn(td, agent="coder", msgs=(), ts="2026-11-01 10:00:00", pull=False):
    (td / "inflight.json").write_text(json.dumps({agent: {
        "ts": ts, "pull": pull, "msgs": [m.as_dict() for m in msgs]}}))


def send(sender, to, text, **meta):
    bus = load_transport(sender)
    try:
        bus.send(to, text, **meta)
    finally:
        bus.close()


def inbox(agent="coder"):
    bus = load_transport(agent)
    try:
        return bus.peek()
    finally:
        bus.close()


def calls(td, n, tool="run_command", args=None):
    for _ in range(n):
        steer.count_call(td, "coder", tool, args or {"CommandLine": "pytest"})


def test_the_same_call_is_named_at_three_five_and_eight(team):
    begin_turn(team)
    seen = []
    for i in range(1, 9):
        calls(team, 1)
        if steer.take_notes(team, "coder"):
            seen.append(i)
    assert seen == [3, 5, 8]


def test_the_note_gets_firmer_and_shows_the_arguments(team):
    begin_turn(team)
    calls(team, 3)
    first = steer.take_notes(team, "coder")
    assert "3 times in a row" in first and "pytest" not in first
    calls(team, 2)
    second = steer.take_notes(team, "coder")
    assert "5 times in a row" in second and "pytest" in second
    assert "end the turn and say what is in the way" in second


def test_any_other_call_between_them_is_not_a_repeat(team):
    begin_turn(team)
    for _ in range(4):
        calls(team, 2)
        calls(team, 1, tool="edit_file", args={"path": "a.py"})
    assert steer.take_notes(team, "coder") == ""


def test_argument_order_does_not_make_a_new_call(team):
    begin_turn(team)
    calls(team, 2, args={"a": 1, "b": 2})
    calls(team, 1, args={"b": 2, "a": 1})
    assert "3 times" in steer.take_notes(team, "coder")


def test_what_the_model_says_about_a_call_does_not_make_it_a_new_call(team):
    begin_turn(team)
    for n, field in enumerate(("toolSummary", "toolAction", "description")):
        calls(team, 1, args={"CommandLine": "echo hi", field: f"try {n}"})
    assert "3 times" in steer.take_notes(team, "coder")


def test_a_new_turn_starts_the_count_again(team):
    begin_turn(team)
    calls(team, 2)
    begin_turn(team, ts="2026-11-01 10:05:00")
    calls(team, 1)
    assert steer.take_notes(team, "coder") == ""


def test_mail_that_arrives_during_a_turn_is_shown_once(team):
    begin_turn(team)
    send("user", "coder", "Stop, wrong branch.")
    note = steer.take_notes(team, "coder")
    assert "from user: Stop, wrong branch." in note and "check_inbox" in note
    assert steer.take_notes(team, "coder") == ""
    send("qa", "coder", "Second thing.")
    assert "from qa: Second thing." in steer.take_notes(team, "coder")


def test_the_mail_a_turn_began_with_is_not_news(team):
    send("user", "coder", "Build it.")
    begin_turn(team, msgs=inbox())
    assert steer.take_notes(team, "coder") == ""


def test_a_quiet_post_waits_for_a_wake(team):
    begin_turn(team)
    send("qa", "coder", "fyi, passed", kind="fyi")
    send("qa", "coder", "chatter", quiet=True)
    assert steer.take_notes(team, "coder") == ""


def test_a_long_message_is_cut_and_many_are_counted(team):
    begin_turn(team)
    send("user", "coder", "x" * 2000)
    for i in range(4):
        send("qa", "coder", f"m{i}")
    note = steer.take_notes(team, "coder")
    assert "x" * 300 + "..." in note and "x" * 301 not in note
    assert "and 2 more" in note


def test_outside_a_supervised_turn_nothing_happens(team):
    send("user", "coder", "hello")
    calls(team, 5)
    assert steer.take_notes(team, "coder") == ""
    assert not (team / "steer").exists()


def test_a_refusal_is_saved_for_what_cannot_wait(team):
    begin_turn(team)
    send("qa", "coder", "a teammate's note")
    calls(team, 3)
    assert steer.take_notes(team, "coder", urgent_only=True) == ""
    calls(team, 2)
    assert "5 times" in steer.take_notes(team, "coder", urgent_only=True)
    send("user", "coder", "Stop.")
    note = steer.take_notes(team, "coder", urgent_only=True)
    assert "from user: Stop." in note
    # What was held back is still there for the next team tool result.
    assert "from qa" in note or "from qa" in steer.take_notes(team, "coder")


def test_a_broken_ledger_or_inbox_means_no_note_and_no_error(team):
    (team / "inflight.json").write_text("{not json")
    calls(team, 3)
    assert steer.take_notes(team, "coder") == ""
    begin_turn(team)

    class Broken:
        def peek(self):
            raise OSError("gone")
    assert steer.take_notes(team, "coder", transport=Broken()) == ""


def test_notes_are_recorded(team):
    begin_turn(team)
    calls(team, 3)
    send("user", "coder", "hi")
    steer.take_notes(team, "coder")
    events = [json.loads(line) for line in (team / "events.jsonl").read_text().splitlines()]
    steered = [e for e in events if e["event"] == "steer"]
    assert steered and steered[0]["agent"] == "coder"
    assert steered[0]["mail"] == 1 and steered[0]["repeat"] == 3


# --- check_inbox ----------------------------------------------------------

def test_check_inbox_mid_turn_does_not_repeat_the_wake_prompt(team):
    send("user", "coder", "Build it.")
    begin_turn(team, msgs=inbox())
    send("qa", "coder", "New finding.")
    bus = load_transport("coder")
    try:
        out = mcp_bus._check_inbox(bus)
    finally:
        bus.close()
    assert "New finding." in out and "Build it." not in out
    assert [m.content for m in inbox()] == ["Build it."], "the supervisor acknowledges that one"


def test_under_pull_the_wake_mail_is_what_check_inbox_is_for(team):
    send("user", "coder", "Build it.")
    begin_turn(team, msgs=inbox(), pull=True)
    bus = load_transport("coder")
    try:
        assert "Build it." in mcp_bus._check_inbox(bus)
    finally:
        bus.close()


def test_a_ledger_from_an_older_supervisor_hides_no_mail(team):
    send("user", "coder", "Build it.")
    (team / "inflight.json").write_text(json.dumps({"coder": {
        "ts": "2026-11-01 10:00:00", "msgs": [m.as_dict() for m in inbox()]}}))
    bus = load_transport("coder")
    try:
        assert "Build it." in mcp_bus._check_inbox(bus)
    finally:
        bus.close()


class Reading(ScriptedRunner):
    """A turn that reads mail mid-turn, then fails or does not."""

    def __init__(self, reply):
        super().__init__({})
        self.reply = reply

    def wake(self, agent, message):
        self.woken.append(agent)
        send("qa", "coder", "Arrived mid-turn.")
        bus = load_transport(agent)
        try:
            mcp_bus._check_inbox(bus)
        finally:
            bus.close()
        return self.reply


def drive(team, runner):
    sup = Supervisor(["coder", "qa"], runner, require_review=False, max_hops=1, quiet=True)
    send("user", "coder", "Build it.")
    try:
        sup.step()
        for _ in range(100):
            if not sup.inflight:
                return sup
            sup.step()
            time.sleep(0.01)
        raise AssertionError("the turn never ended")
    finally:
        sup.close()


def test_mail_read_mid_turn_goes_back_if_the_turn_fails(team):
    drive(team, Reading("[error: 503 unavailable]"))
    assert "Arrived mid-turn." in [m.content for m in inbox()]


def test_mail_read_mid_turn_stays_read_if_the_turn_succeeds(team):
    drive(team, Reading("done"))
    assert inbox() == []
    assert receipts.taken(team, "coder") == []


def test_the_ledger_says_how_mail_reached_the_turn(team):
    class Held(ScriptedRunner):
        def __init__(self):
            super().__init__({})

        def wake(self, agent, message):
            for _ in range(200):
                if steer.turn_of(team, agent):
                    break
                time.sleep(0.01)
            self.ledger = json.loads((team / "inflight.json").read_text())
            return "ok"

    runner = Held()
    drive(team, runner)
    assert runner.ledger["coder"]["pull"] is False


# --- hook_step and the server footer ---------------------------------------

def hook(event, payload, env):
    r = subprocess.run([sys.executable, "-m", "ateam.hook_step", event],
                       input=json.dumps(payload), capture_output=True, text=True,
                       env=env, cwd=ROOT, check=False)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.fixture
def env(team):
    return {"PATH": "/usr/bin:/bin", "ATEAM_TEAM_DIR": str(team), "ATEAM_AGENT": "coder",
            "ATEAM_DURABLE_DIR": str(team.parent / "durable")}


def test_the_agy_hooks_count_calls_and_inject_what_is_due(team, env):
    begin_turn(team)
    step = {"conversationId": "c1", "invocationNum": 4}
    call = {"conversationId": "c1", "toolCall": {"name": "run_command",
                                                 "args": {"CommandLine": "pytest"}}}
    assert hook("model", step, env) == {}
    for _ in range(3):
        assert hook("tool", call, env) == {}
    out = hook("model", step, env)
    text = out["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "run_command call 3 times" in text
    assert hook("model", step, env) == {}


def test_the_hook_finds_the_agent_by_its_conversation(team, env):
    del env["ATEAM_AGENT"]
    (team / "conversations.json").write_text(json.dumps({"coder": "c1"}))
    begin_turn(team)
    send("user", "coder", "Stop.")
    out = hook("model", {"conversationId": "c1"}, env)
    assert "Stop." in out["injectSteps"][0]["systemMessage"]["systemMessage"]


def test_a_persons_own_session_is_left_alone(team, env):
    del env["ATEAM_AGENT"]
    begin_turn(team)
    assert hook("model", {"conversationId": "somebody-else"}, env) == {}
    assert hook("tool", {"toolCall": {"name": "x", "args": {}}}, env) == {}


def test_nonsense_on_stdin_does_not_fail_the_hook(env):
    r = subprocess.run([sys.executable, "-m", "ateam.hook_step", "model"], input="{oops",
                       capture_output=True, text=True, env=env, cwd=ROOT, check=False)
    assert r.returncode == 0 and json.loads(r.stdout) == {}


def test_a_team_tool_result_carries_a_footer(monkeypatch, capsys):
    calls_in = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "t", "arguments": {}}}]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(c) + "\n" for c in calls_in)))
    tools = [{"name": "t", "description": "", "inputSchema": {"type": "object", "properties": {}}}]
    mcp_base.serve("s", tools, lambda n, a: "result", footer=lambda: "[a note]")
    text = json.loads(capsys.readouterr().out)["result"]["content"][0]["text"]
    assert text == "result\n\n[a note]"


def test_the_antigravity_sdk_refuses_one_call_to_say_what_cannot_wait(team):
    agent = pytest.importorskip("ateam.sdk_agent")
    conf = agent.build_config(team.parent / "ws", name="coder", with_bus=True, use_mcp=True)
    hook_fn = next(h for h in conf.hooks if "steer_turn" in repr(vars(h)))

    def run(call):
        return asyncio.run(hook_fn.run(None, call))

    begin_turn(team)
    call = SimpleNamespace(name="run_command", args={"CommandLine": "pytest"})
    assert run(call).allow is True
    send("user", "coder", "Stop.")
    refused = run(SimpleNamespace(name="view_file", args={"AbsolutePath": "/a"}))
    assert refused.allow is False
    assert "from user: Stop." in refused.message and "did not run" in refused.message
    assert run(SimpleNamespace(name="view_file", args={"AbsolutePath": "/b"})).allow is True
    send("user", "coder", "Also this.")
    assert run(SimpleNamespace(name="mcp_ateam_bus_check_inbox", args={})).allow is True


def test_the_plugin_ships_the_hooks():
    hooks = json.loads((ROOT / "plugin" / "hooks.template.json").read_text())["a-team-steer"]
    assert hooks["PostToolUse"][0]["hooks"][0]["command"].endswith("-m ateam.hook_step tool")
    assert hooks["PreInvocation"][0]["command"].endswith("-m ateam.hook_step model")


def test_message_keys_ignore_how_the_message_was_carried():
    a = Message(ts="t", sender="s", to="x", content="c", id="1", quiet=True)
    b = Message(ts="t", sender="s", to="x", content="c")
    assert steer._mail_key(a) == steer._mail_key(b)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
