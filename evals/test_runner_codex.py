"""A team on OpenAI: the runner, through the real Codex app-server, against a local API.

Asked for: a second vendor beside Claude, for model diversity. These run the
real `codex app-server`, with the real MCP servers and Codex's own sandbox.
Only the Responses API is fake (fake_openai.py), so no key is needed and
nothing is spent. Codex 0.166 or later is needed: ATEAM_TEST_CODEX names one,
or `codex` on PATH; the tests are skipped without it.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam.runner_codex import FEATURES_OFF, CodexRunner, _error_text  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402
from fake_openai import FakeOpenAI, js, status  # noqa: E402


def _codex():
    path = os.environ.get("ATEAM_TEST_CODEX") or shutil.which("codex")
    if not path:
        return None
    try:
        ok = subprocess.run([path, "app-server", "--help"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if ok.returncode == 0 else None


CODEX = _codex()
needs_codex = pytest.mark.skipif(not CODEX, reason="no codex with app-server (ATEAM_TEST_CODEX)")


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (td / "roster.json").write_text(json.dumps({"workspaces": [str(work)], "agents": [
        {"name": "coder", "role": "implements"},
        {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.chdir(tmp_path)
    return td


@pytest.fixture
def api(request):
    fake = FakeOpenAI(getattr(request, "param", ["done"]))
    yield fake
    fake.close()


def runner(api, **config):
    return CodexRunner({"timeout": 60, "codex": CODEX,
                        "provider": {"base_url": api.url + "/v1"}, **config})


def events(td, kind):
    if not (td / "events.jsonl").exists():
        return []
    rows = [json.loads(l) for l in (td / "events.jsonl").read_text().splitlines() if l.strip()]
    return [e for e in rows if e.get("event") == kind]


def developer_text(req):
    return json.dumps([i for i in req["input"] if i.get("role") == "developer"])


def tool_names(req):
    """Every tool the request offers, nested ones included."""
    return json.dumps([i for i in req["input"] if i.get("type") == "additional_tools"]
                      + req.get("tools", []))


@needs_codex
def test_a_turn_reaches_the_model_with_the_brief(team, api):
    assert runner(api).wake("coder", "build it") == "done"
    req = api.requests[0]
    assert req["model"] == "gpt-6.1-sol"
    brief = developer_text(req)
    assert "coder" in brief and "implements" in brief, "the brief did not reach the model"
    auth = {k.lower(): v for k, v in api.headers.items()}.get("authorization")
    assert auth == "Bearer not-a-real-key", "the key from the environment was not used"
    turn = events(team, "turn")[-1]
    assert turn["model"] == "gpt-6.1-sol" and turn["cost_usd"] is None
    assert turn["input_tokens"] == 100 and turn["cache_read_tokens"] == 1000


@needs_codex
def test_the_reviewer_gets_the_strong_model_and_the_roster_can_name_one(team, api):
    runner(api).wake("qa", "review it")
    assert api.requests[-1]["model"] == "gpt-6-astra"
    doc = json.loads((team / "roster.json").read_text())
    doc["agents"][0]["model"] = "gpt-6-luna"
    (team / "roster.json").write_text(json.dumps(doc))
    runner(api).wake("coder", "build it")
    assert api.requests[-1]["model"] == "gpt-6-luna"


@needs_codex
def test_an_agent_keeps_its_thread_across_wakes(team, api):
    r = runner(api)
    r.wake("coder", "first")
    first = r.conversation_id("coder")
    assert first, "the thread was not bound to the agent"
    runner(api).wake("coder", "second")
    assert runner(api).conversation_id("coder") == first
    said = json.dumps(api.requests[-1]["input"])
    assert "first" in said and "second" in said, "the second turn did not resume the first"


@needs_codex
def test_a_lost_thread_starts_a_new_one(team, api):
    r = runner(api)
    r.remember_conversation("coder", "01a19fce-0000-7000-8000-000000000000")
    assert r.wake("coder", "go") == "done"
    assert r.conversation_id("coder") != "01a19fce-0000-7000-8000-000000000000"


@needs_codex
@pytest.mark.parametrize("api", [[js(
    "const r = await tools.mcp__ateam_bus__send_to_teammate("
    "{to: 'qa', content: 'please review', kind: 'review'}); text(JSON.stringify(r));"),
    "sent"]], indirect=True)
def test_team_tools_work_and_are_on_the_record(team, api):
    assert runner(api).wake("coder", "ask qa") == "sent"
    bus = load_transport("qa")
    try:
        assert any("please review" in m.content for m in bus.peek()), "qa got nothing"
    finally:
        bus.close()
    calls = events(team, "tool_call")
    assert calls and calls[-1]["tool"] == "send_to_teammate" and not calls[-1]["error"]


@needs_codex
@pytest.mark.parametrize("api", [[js(
    "const r = await tools.exec_command({cmd: 'echo made > here.txt; "
    "touch /etc/ateam_probe; echo key=[$OPENAI_API_KEY]'}); text(JSON.stringify(r));"),
    "ok"]], indirect=True)
def test_commands_write_only_in_the_workspace_and_never_see_the_key(team, api):
    runner(api).wake("coder", "go")
    work = Path(json.loads((team / "roster.json").read_text())["workspaces"][0])
    assert (work / "here.txt").read_text().strip() == "made"
    assert not Path("/etc/ateam_probe").exists()
    out = events(team, "tool_call")[-1]
    assert out["tool"] == "run_command"
    assert "Read-only file system" in (out["result"] or "") + (out["error"] or "")
    assert "key=[]" in (out["result"] or "") + (out["error"] or ""), "the key reached the shell"


@needs_codex
def test_a_withheld_tool_is_not_offered(team, api):
    doc = json.loads((team / "roster.json").read_text())
    doc["agents"][0]["tools_off"] = ["run_command", "search_web"]
    (team / "roster.json").write_text(json.dumps(doc))
    runner(api).wake("coder", "go")
    offered = tool_names(api.requests[-1])
    shell = ("### `exec_command`", '"name": "exec_command"', '"name": "shell"')
    assert not any(s in offered for s in shell), "the shell was offered"
    assert '"web_search"' not in offered
    runner(api).wake("qa", "go")
    assert any(s in tool_names(api.requests[-1]) for s in shell), "only coder withholds it"


@needs_codex
@pytest.mark.parametrize("api", [[status(429, "You exceeded your current quota.",
                                         "insufficient_quota")]], indirect=True)
def test_an_empty_account_fails_in_words_the_supervisor_stops_on(team, api, monkeypatch):
    monkeypatch.setattr("ateam.runner.RETRY_ATTEMPTS", 1)
    out = runner(api).wake("coder", "go")
    assert out.startswith("[error:") and "quota" in out.lower(), out
    assert events(team, "failure"), "the failure is not on the record"


def test_codex_errors_are_worded_for_retry_and_the_budget_stop():
    from ateam import config
    from ateam.runner import is_retryable
    halt = _error_text({"message": "out", "codexErrorInfo": "usageLimitExceeded"})
    assert any(p in halt.lower() for p in config.BUDGET_HALT_PATTERNS)
    assert is_retryable(_error_text({"message": "busy", "codexErrorInfo": "rateLimitExceeded"}))
    assert not is_retryable(_error_text({"message": "no", "codexErrorInfo": "unauthorized"}))


@needs_codex
@pytest.mark.parametrize("api", [[js("text('step')"), js("text('step')"), "done"]], indirect=True)
def test_a_turn_over_its_token_limit_stops_and_says_so(team, api):
    out = runner(api, max_turn_tokens=50).wake("coder", "go")
    assert out == "[stopped: limit of 50 tokens]", out
    assert len(api.requests) < 3, "the turn went on past its limit"
    bus = load_transport("user")
    try:
        assert any("limit of 50 tokens" in m.content for m in bus.peek())
    finally:
        bus.close()


@needs_codex
def test_no_key_says_so_before_starting_anything(team, api, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    out = runner(api).wake("coder", "go")
    assert "set OPENAI_API_KEY" in out and not api.requests


@needs_codex
def test_a_turn_can_be_cancelled(team, api):
    api.hold = threading.Event()
    r = runner(api)
    handle = r.begin("coder", "go")
    for _ in range(100):
        if api.requests:
            break
        time.sleep(0.1)
    t0 = time.monotonic()
    r.cancel(handle)
    api.hold.set()
    r.wait_turn(30)
    assert time.monotonic() - t0 < 30


def begin_turn(td, agent="coder"):
    """What the supervisor's ledger says while a turn runs."""
    (td / "inflight.json").write_text(json.dumps({agent: {"ts": "2026-11-01 10:00:00", "msgs": []}}))


@needs_codex
@pytest.mark.parametrize("api", [[js("await tools.exec_command({cmd: 'echo hi'})")] * 4 + ["done"]],
                         indirect=True)
def test_the_same_call_three_times_reaches_the_model_as_a_note(team, api):
    begin_turn(team)
    assert runner(api).wake("coder", "go") == "done"
    assert "3 times in a row" not in json.dumps(api.requests[2]["input"])
    assert "3 times in a row" in json.dumps(api.requests[-1]["input"])


@needs_codex
@pytest.mark.parametrize("api", [[js("await tools.mcp__ateam_bus__list_teammates({})"),
                                  js("await tools.exec_command({cmd: 'true'})"), "done"]],
                         indirect=True)
def test_mail_that_arrives_mid_turn_reaches_the_model_at_the_next_step(team, api):
    begin_turn(team)
    api.hold = threading.Event()        # the first reply waits for the mail

    def deliver():
        while not api.requests:
            time.sleep(0.01)
        bus = load_transport("user")
        bus.send("coder", "Stop, wrong branch.")
        bus.close()
        api.hold.set()

    threading.Thread(target=deliver, daemon=True).start()
    assert runner(api).wake("coder", "go") == "done"
    assert "Stop, wrong branch." in json.dumps(api.requests[-1]["input"])


def test_the_surface_leaves_out_what_an_unattended_agent_must_not_have():
    for f in ("computer_use", "browser_use", "plugins", "apps", "daemon_auto_start"):
        assert f in FEATURES_OFF


@needs_codex
def test_the_command_line_is_one_codex_accepts(team, api, tmp_path):
    r = runner(api)
    spec, _, spaces = r._spec("coder")
    argv = r._argv("coder", spec, spaces) + ["--print-startup-config"]
    out = subprocess.run(argv, capture_output=True, text=True, env=r._env("coder"), timeout=60)
    assert out.returncode == 0 and '"error"' not in out.stdout, out.stdout[-2000:] + out.stderr[-2000:]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
