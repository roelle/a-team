"""Standing duties: work that never finishes, on a schedule of its own.

Asked for: "Standing duties have no home. Some work never finishes. If a
turn fails, the task is closed - or if the host is down when the reminder
fires, the duty stops silently. No record shows it was supposed to run and
no agent hears about the miss." These pin that the schedule is the sweep's
to keep, that finishing a pass records a run and leaves the duty open, and
that a run never done or never delivered is on the record and in the next
reminder.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import duties, tasks  # noqa: E402
from ateam.duties import DutyError  # noqa: E402
from ateam.record import Record  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": "watcher", "role": "sweeps the inbox"}, {"name": "qa", "role": "reviews"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    return td


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, content, kind=None, task_id=None):
        self.sent.append({"to": to, "content": content, "kind": kind, "task_id": task_id})
        return f"[delivered to {to}]"


def at(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def test_a_schedule_is_read_when_it_is_set():
    assert duties.parse("2h") == "2h"
    assert duties.parse("weekdays 8:00 America/Los_Angeles") == "weekdays 08:00 America/Los_Angeles"
    assert duties.parse("mon,thu 09:30 UTC") == "mon,thu 09:30 UTC"
    for bad, says in (("1m", "loop"), ("sometimes", "not a schedule"),
                      ("daily 25:00 UTC", "time of day"), ("daily 08:00 Mars/Base", "not a time zone"),
                      ("fortnightly 08:00 UTC", "not days")):
        with pytest.raises(DutyError, match=says):
            duties.parse(bad)


def test_a_time_of_day_without_a_zone_takes_this_hosts(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    assert duties.parse("daily 07:00") == "daily 07:00 Europe/Paris"


def test_a_weekday_duty_skips_the_weekend_in_its_own_zone():
    friday_evening = datetime(2026, 11, 6, 18, 0, tzinfo=LA).timestamp()
    nxt = duties.next_after("weekdays 08:00 America/Los_Angeles", friday_evening)
    assert datetime.fromtimestamp(nxt, LA) == datetime(2026, 11, 9, 8, 0, tzinfo=LA)


def test_a_duty_needs_someone_to_run_it(team):
    with pytest.raises(tasks.TaskError, match="needs an owner"):
        tasks.create(team, "", "sweep the inbox", created_by="user", every="1h")
    t = tasks.create(team, "", "sweep the inbox", created_by="watcher", every="1h")
    assert t["owner"] == "watcher" and t["every"] == "1h"
    assert t["check_after"] <= tasks._now(), "a duration duty first runs now"


def test_a_run_is_delivered_and_the_next_one_set_from_the_schedule(team):
    t = tasks.create(team, "", "sweep the inbox", owner="watcher", created_by="user",
                     every="1h", note="read new mail, leave it unread")
    mail = Mail()
    tasks.sweep(team, mail)
    got = tasks.get(team, t["id"])
    assert mail.sent[0]["to"] == "watcher" and mail.sent[0]["kind"] == "reminder"
    assert "Standing duty" in mail.sent[0]["content"] and "leave it unread" in mail.sent[0]["content"]
    assert got["run_due"] == t["check_after"]
    assert duties._epoch(got["check_after"]) == duties._epoch(t["check_after"]) + 3600
    tasks.sweep(team, mail)
    assert len(mail.sent) == 1, "nothing more until the next run is due"


def test_finishing_a_pass_records_the_run_and_the_duty_stays_open(team):
    t = tasks.create(team, "", "sweep the inbox", owner="watcher", created_by="user", every="1h")
    tasks.sweep(team, Mail())
    got = tasks.complete(team, t["id"], "watcher", "3 new, 1 needs the user")
    assert got["status"] == "running" and got["every"] == "1h"
    assert got["last_note"] == "3 new, 1 needs the user" and got["run_due"] is None
    assert got["check_after"], "the next run is still set"
    runs = [r for r in tasks.read(team) if r.get("run") == "done"]
    assert runs and runs[0]["run_for"] == t["check_after"]


def test_a_run_never_done_is_missed_and_its_owner_told(team):
    t = tasks.create(team, "", "watch the dashboard", owner="watcher", created_by="user", every="1h")
    mail = Mail()
    tasks.sweep(team, mail)                       # out, and the turn failed
    first = tasks.get(team, t["id"])
    tasks.sweep(team, mail, now=first["check_after"])
    got = tasks.get(team, t["id"])
    assert got["missed"] == 1 and got["last_missed"] == first["run_due"]
    assert "never marked done" in mail.sent[-1]["content"]
    assert got["status"] not in tasks.TERMINAL, "a failed turn does not end the duty"


def test_runs_that_fell_due_while_nothing_swept_are_counted_and_caught_up_once(team):
    t = tasks.create(team, "", "watch the dashboard", owner="watcher", created_by="user", every="1h")
    mail = Mail()
    down_until = duties._epoch(t["check_after"]) + 5 * 3600 + 60       # five hours later
    tasks.sweep(team, mail, now=at(down_until))
    got = tasks.get(team, t["id"])
    assert len(mail.sent) == 1, "one catch-up run, not one per miss"
    assert got["missed"] == 5 and got["missed_total"] == 5
    assert "5 runs" in mail.sent[0]["content"] and "catches up" in mail.sent[0]["content"]
    assert got["run_due"] == at(duties._epoch(t["check_after"]) + 5 * 3600)
    assert duties._epoch(got["check_after"]) == duties._epoch(t["check_after"]) + 6 * 3600
    tasks.complete(team, t["id"], "watcher", "caught up")
    assert tasks.get(team, t["id"])["missed"] == 0 and tasks.get(team, t["id"])["missed_total"] == 5


def test_an_agent_cannot_close_a_duty_by_finishing_a_pass(team):
    t = tasks.create(team, "", "sweep the inbox", owner="watcher", created_by="user", every="1h")
    with pytest.raises(tasks.TaskError, match="standing duty"):
        tasks.update(team, t["id"], "watcher", status="done")
    ended = tasks.update(team, t["id"], "user", status="done", note="not needed now")
    assert ended["status"] == "done" and ended["check_after"] is None


def test_every_empty_turns_a_duty_back_into_a_task(team):
    t = tasks.create(team, "", "sweep the inbox", owner="watcher", created_by="user", every="1h")
    got = tasks.update(team, t["id"], "watcher", every="")
    assert got["every"] is None and got["check_after"] is None
    assert tasks.complete(team, t["id"], "watcher", "done for good")["status"] == "done"


def test_a_duty_behind_shows_when_nothing_is_sweeping(team):
    t = tasks.create(team, "", "watch the dashboard", owner="watcher", created_by="user", every="1h")
    assert duties.late(t) is None
    assert "has not been delivered" in duties.late(t, duties._epoch(t["check_after"]) + 2 * 3600)


def test_an_agent_makes_and_runs_a_duty_through_its_tools(team):
    from ateam import mcp_tasks
    out = mcp_tasks._create_task("watcher", team, {"title": "sweep the inbox", "every": "2h",
                                                   "note": "leave mail unread"})
    tid = next(iter(tasks.snapshot(team)))
    assert "standing duty, every 2h" in out
    tasks.sweep(team, Mail())
    out = mcp_tasks._complete_task("watcher", team, {"task_id": tid, "note": "2 new, none urgent"})
    assert out.startswith("[run recorded") and "stays open" in out
    out = mcp_tasks._create_task("watcher", team, {"title": "too often", "every": "1m"})
    assert out.startswith("[error") and "loop" in out
    names = {t["name"]: t for t in mcp_tasks.TOOLS}
    assert "every" in names["create_task"]["inputSchema"]["properties"]
    assert "every" in names["update_task"]["inputSchema"]["properties"]


def test_a_duty_behind_is_waiting_on_the_operator(team):
    t = tasks.create(team, "", "watch the dashboard", owner="watcher", created_by="user", every="1h")
    assert not [r for r in Record(team).needs_you() if r["kind"] == "duty"]
    mail = Mail()
    tasks.sweep(team, mail)
    tasks.sweep(team, mail, now=tasks.get(team, t["id"])["check_after"])
    assert not [r for r in Record(team).needs_you() if r["kind"] == "duty"], "one miss is the owner's"
    tasks.sweep(team, mail, now=tasks.get(team, t["id"])["check_after"])
    row = next(r for r in Record(team).needs_you() if r["kind"] == "duty")
    assert row["owner"] == "watcher" and row["missed"] == 2 and "not done" in row["reason"]


def test_the_operator_makes_a_duty_and_the_app_is_sent_its_runs(team):
    from ateam import actions
    from ateam.chat.server import App
    t = actions.create_task(team, "", "sweep the inbox", owner="watcher", every="weekdays 08:00 UTC")
    assert t["every"] == "weekdays 08:00 UTC"
    told = [json.loads(l) for l in (team / "inbox" / "watcher.jsonl").read_text().splitlines()]
    assert "standing duty" in told[-1]["content"] and told[-1]["kind"] == "fyi"
    assert actions.update_task(team, t["id"], every="")["every"] is None
    assert {"every", "run_due", "last_done", "last_note", "missed", "missed_total",
            "last_missed"} <= set(App.TASK_FIELDS)


def test_the_board_gives_duties_a_column_of_their_own():
    board = (ROOT / "ateam/chat/static/board.js").read_text()
    assert 'key: "duties", title: "Standing"' in board and "function inCol" in board
    assert "function dutyBadge" in board and 'name="every"' in board
    app = (ROOT / "ateam/chat/static/app.js").read_text()
    assert 'n.kind === "duty"' in app


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
