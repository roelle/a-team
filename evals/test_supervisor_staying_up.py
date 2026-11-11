"""What a person running a team for days asked of the supervisor:

1. "Heartbeat while waiting on a turn. Supervisor goes silent during
   distills and sweeps so the app shows 'no supervisor' and the watcher
   pages user." These pin that the heartbeat stays fresh while a distill
   waits, says what it waits on, and that a one-off distill never claims
   to be the team's supervisor.
2. "Throttle 'skipping auto-cycle' log: it writes a line every second and
   counts quiet messages as pending work." These pin that quiet mail does
   not hold off a cycle, and that mail that does is said once, not every
   pass.
3. "Guard the supervisor's log writes: a brief disk-full kills the daemon."
   These pin that a write that fails is dropped and counted, and the count
   said once writing works again.
4. "Treat a turn that times out after repeated 503s as a capacity failure,
   so it falls back to another model." These pin that such a timeout says
   what refused it, in the words the fallback reads, and moves to the next
   model without waiting the same one out again.
5. "Allow task limits on create-assign only. The current setting also hides
   tasks from list_tasks, which blinds some agents." These pin that
   task_projects_limit "create_assign" leaves listing, claiming and
   finishing open, keeps creating and assigning limited, and that the
   default is unchanged.
"""
import errno
import io
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import config, heartbeat, mcp_tasks, quota, tasks  # noqa: E402
from ateam import runner as runner_lib  # noqa: E402
from ateam import supervisor as sup_mod  # noqa: E402
from ateam.runner_agy import AgyRunner  # noqa: E402
from ateam.supervisor import Supervisor  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402
from fixture_runner import ScriptedRunner  # noqa: E402

AGENTS = ["tpm", "coder"]


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "tpm", "role": "coordinates", "is_principal": True},
        {"name": "coder", "role": "implements", "model": "big"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "0")
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    monkeypatch.setattr(config, "WAKE_BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr(config, "WAKE_BACKOFF_MAX_S", 0.0)
    monkeypatch.setattr(runner_lib, "RETRY_BASE_SECONDS", 0.0)
    return td


# --- 1. the heartbeat while a turn is waited on --------------------------------

class SlowDistill(ScriptedRunner):
    """A distill that outlasts several heartbeats, looking at the heartbeat
    from inside the turn."""

    def __init__(self, td):
        super().__init__({})
        self.hb, self.seen = heartbeat.path(td), []

    def wake(self, agent, message):
        self.hb.unlink(missing_ok=True)
        time.sleep(0.3)
        self.seen.append(json.loads(self.hb.read_text()) if self.hb.exists() else None)
        return "noted"


def test_a_distill_keeps_the_heartbeat_fresh_and_says_what_it_waits_on(team, monkeypatch):
    monkeypatch.setattr(sup_mod, "BEAT_WHILE_BLOCKED_S", 0.05)
    runner = SlowDistill(team)
    sup = Supervisor(AGENTS, runner, quiet=True)
    try:
        assert sup._claim_team()
        sup.distill("coder")
        assert runner.seen[0]["busy"] == "distilling coder", "written again while the turn ran"
        assert heartbeat.running(team)["pid"] == os.getpid()
        assert heartbeat.running(team)["busy"] is None, "and cleared once it ended"
    finally:
        sup.close()


def test_distilling_several_at_once_keeps_it_fresh_too(team, monkeypatch):
    monkeypatch.setattr(sup_mod, "BEAT_WHILE_BLOCKED_S", 0.05)
    runner = SlowDistill(team)
    sup = Supervisor(AGENTS, runner, quiet=True, poll=0.05)
    try:
        assert sup._claim_team()
        for a in AGENTS:
            sup.observer.record_turn(a, "c", 1.0)
        sup.distill_all()
        assert runner.seen and all(s and s["busy"].startswith("distilling") for s in runner.seen)
    finally:
        sup.close()


def test_a_one_off_distill_does_not_pass_for_the_teams_supervisor(team, monkeypatch):
    monkeypatch.setattr(sup_mod, "BEAT_WHILE_BLOCKED_S", 0.05)
    runner = SlowDistill(team)
    sup = Supervisor(AGENTS, runner, quiet=True)
    try:
        sup.distill("coder")                # never claimed the team
    finally:
        sup.close()
    assert runner.seen == [None] and heartbeat.running(team) is None


def test_the_app_shows_what_the_supervisor_is_busy_with():
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    assert "b.busy ? ` · ${b.busy}`" in app


# --- 2. auto-cycle and the mail it waits for -----------------------------------

def cycling(team, capsys):
    runner = ScriptedRunner({})
    runner.remember_conversation("coder", "conv")
    sup = Supervisor(AGENTS, runner, quiet=False)
    cycled = []
    sup.cycle = lambda a: cycled.append(a) or (0, 0)
    sup.observer.record_turn("coder", "conv", 1.0, input_tokens=500_000)
    capsys.readouterr()
    return sup, cycled


def test_quiet_mail_does_not_hold_off_a_cycle(team, capsys):
    load_transport("tpm").send("coder", "fyi: the build is green", kind="fyi")
    sup, cycled = cycling(team, capsys)
    try:
        sup.auto_cycle()
    finally:
        sup.close()
    assert cycled == ["coder"]


def test_mail_that_will_wake_it_is_said_once_not_every_pass(team, capsys):
    load_transport("tpm").send("coder", "please fix the build")
    sup, cycled = cycling(team, capsys)
    try:
        for _ in range(50):
            sup.auto_cycle()
        said = [l for l in capsys.readouterr().out.splitlines() if "not cycling coder" in l]
        assert cycled == [] and len(said) == 1 and "1 message(s) waiting" in said[0]
        inbox = load_transport("coder")
        inbox.acknowledge(inbox.fetch())    # handled
        sup.auto_cycle()
        assert cycled == ["coder"]
    finally:
        sup.close()


# --- 3. a log that cannot be written -------------------------------------------

class FullDisk(io.StringIO):
    full = True

    def write(self, text):
        if self.full:
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(text)

    def flush(self):
        if self.full:
            raise OSError(errno.ENOSPC, "No space left on device")


def test_a_full_disk_drops_log_lines_and_says_so_after(team, monkeypatch):
    disk = FullDisk()
    monkeypatch.setattr(sys, "stdout", disk)
    sup_mod.guard_output()
    sup = Supervisor(AGENTS, ScriptedRunner({}), quiet=False)
    try:
        sup._log("one")
        print("two", flush=True)           # a runner's own print, in the same process
        disk.full = False
        sup._log("three")
    finally:
        sup.close()
    out = disk.getvalue()
    assert "2 log line(s) lost: OSError" in out and "No space left" in out
    assert out.rstrip().endswith("three")


def test_the_daemon_guards_its_output():
    src = (ROOT / "ateam/supervisor.py").read_text()
    daemon = src[src.index("elif args.daemon:"):src.index("sup.run_forever()")]
    assert "guard_output()" in daemon


# --- 4. a turn that ran out its time being refused -----------------------------

def test_a_timeout_after_repeated_refusals_says_so():
    seen = ["starting", "API error 503 UNAVAILABLE: high demand, retrying",
            "tool ran", "503 Service Unavailable, retrying in 8s"]
    note = runner_lib.capacity_note(seen)
    err = f"[error: the turn ran past 600s{note}]"
    assert "refused it 2 times for capacity" in err
    assert runner_lib.is_capacity(err) and runner_lib.is_capacity_timeout(err)
    assert runner_lib.capacity_note(seen[:2]) == "", "one refusal is not a pattern"
    assert not runner_lib.is_capacity("[error: the turn ran past 600s]"), "a slow turn is still slow"


def test_it_falls_back_at_once_and_is_not_waited_out_without_one(team):
    full = "[error: the turn ran past 600s after the provider refused it 4 times for capacity (last: 503)]"

    class Timing(ScriptedRunner):
        def __init__(self):
            super().__init__({})
            self.ran = []

        def wake(self, agent, message):
            def turn():
                m, _ = self.resolved_model(agent, "big")
                self.ran.append(m)
                return full if m == "big" else "ok"
            return runner_lib.with_retry(turn, attempts=4,
                                         on_capacity=lambda e: self.cool_down(agent, e))

    r = Timing()
    assert r.wake("coder", "go") == full
    assert r.ran == ["big"], "no fallback named: one timeout, not four in a row"
    doc = json.loads((team / "roster.json").read_text())
    doc["fallback_models"] = ["small"]
    (team / "roster.json").write_text(json.dumps(doc))
    r.ran.clear()
    assert r.wake("coder", "go") == "ok"
    assert r.ran == ["big", "small"] and quota.cooling(team, "big")


FAKE_AGY = """#!{python}
import sys, time
sys.stderr.write("503 UNAVAILABLE: high demand, retrying\\n")
sys.stderr.flush()
sys.stderr.write("503 UNAVAILABLE: high demand, retrying\\n")
sys.stderr.flush()
time.sleep(30)
"""


def test_an_agy_turn_that_timed_out_on_503s_moves_to_the_next_model(team, tmp_path):
    binary = tmp_path / "agy"
    binary.write_text(FAKE_AGY.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    doc = json.loads((team / "roster.json").read_text())
    doc["fallback_models"] = ["small"]
    (team / "roster.json").write_text(json.dumps(doc))
    runner = AgyRunner({"binary": str(binary), "timeout": 2})
    reply = runner.wake("coder", "do the thing")
    assert "timed out after 2s" in reply and "refused it 2 times for capacity" in reply
    assert runner_lib.is_capacity(reply)
    assert runner.cool_down("coder", reply), "the supervisor's fallback takes it"
    assert runner._past_cooldowns("coder", "big") == "small"


def test_every_runner_that_can_see_the_refusals_reports_them():
    for name, needle in (("runner_claude", 'm.subtype == "api_retry"'),
                         ("runner_codex", "state['refusals'] + srv.stderr"),
                         ("runner_acp", "capacity_note(conn.stderr)"),
                         ("runner_agy", "capacity_note(said.splitlines())")):
        assert needle in (ROOT / f"ateam/{name}.py").read_text(), name


# --- 5. task projects limited on create and assign only ------------------------

@pytest.fixture
def scoped(team):
    doc = json.loads((team / "roster.json").read_text())
    doc["agents"][1].update(allowed_task_projects=["web"])
    doc["agents"].append({"name": "qa", "role": "reviews"})
    (team / "roster.json").write_text(json.dumps(doc))
    home = tasks.create(team, "home", "fix the boiler", created_by="user")
    web = tasks.create(team, "web", "fix the form", created_by="user")
    return team, doc, home, web


def limit(team, doc, how):
    doc["agents"][1]["task_projects_limit"] = how
    (team / "roster.json").write_text(json.dumps(doc))


def test_by_default_the_limit_still_holds_for_everything(scoped):
    team, doc, home, web = scoped
    out = mcp_tasks._list_tasks(team, {}, "coder")
    assert "fix the form" in out and "fix the boiler" not in out
    assert mcp_tasks._claim_task("coder", team, {"task_id": home["id"]}).startswith("[refused:")


def test_create_assign_leaves_reading_and_working_open(scoped):
    team, doc, home, web = scoped
    limit(team, doc, "create_assign")
    out = mcp_tasks._list_tasks(team, {}, "coder")
    assert "fix the form" in out and "fix the boiler" in out
    assert mcp_tasks._list_tasks(team, {"under": home["id"]}, "coder").count("fix the boiler") == 1
    assert mcp_tasks._claim_task("coder", team, {"task_id": home["id"]}).startswith("[claimed]")
    out = mcp_tasks._update_task("coder", team, {"task_id": home["id"], "note": "valve is stuck"})
    assert not out.startswith("[refused:")
    assert not mcp_tasks._complete_task("coder", team, {"task_id": home["id"], "note": "done"}) \
        .startswith("[refused:")


def test_create_assign_still_limits_creating_and_assigning(scoped):
    team, doc, home, web = scoped
    limit(team, doc, "create_assign")
    assert mcp_tasks._create_task("coder", team, {"title": "x", "project": "home"}).startswith("[refused:")
    assert mcp_tasks._create_task("coder", team, {"title": "y", "parent": home["id"]}).startswith("[refused:")
    assert mcp_tasks._update_task("coder", team, {"task_id": home["id"], "owner": "tpm"}) \
        .startswith("[refused:")
    assert mcp_tasks._update_task("coder", team, {"task_id": web["id"], "parent": home["id"]}) \
        .startswith("[refused:")
    assert not mcp_tasks._update_task("coder", team, {"task_id": web["id"], "owner": "tpm"}) \
        .startswith("[refused:")
    assert tasks.get(team, web["id"])["owner"] == "tpm"
    assert "[task created" in mcp_tasks._create_task("coder", team, {"title": "z"})


def test_create_assign_can_be_set_for_the_whole_team(scoped):
    team, doc, home, web = scoped
    doc["policy"] = {"task_projects_limit": "create-assign"}
    (team / "roster.json").write_text(json.dumps(doc))
    assert "fix the boiler" in mcp_tasks._list_tasks(team, {}, "coder")
    limit(team, doc, "all")
    assert "fix the boiler" not in mcp_tasks._list_tasks(team, {}, "coder"), "the agent's own wins"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
