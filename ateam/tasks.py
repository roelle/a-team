"""Where long-running work is tracked, so an agent never has to hold it in its head.

## Why this file exists

The team was running simulations that take hours to finish, and the pattern
that emerged around that was an agent scheduling an external cron job to poke
itself later. That works, but it puts the schedule outside every record this
project keeps: `ateam.doctor` cannot see it, a restart does not know it
existed, and a second agent asking "is anything still running?" has nowhere to
look but the first agent's memory of having set it up.

`retro_store.py` solved the identical problem for retrospective answers: write
to an append-only file instead of holding state where only one process knows
about it, and let everything else -- including a process that starts back up
after a power cycle -- read the same file. A task here is one more append-only
record, folded to current state the same way.

## Reminders, and who delivers them

Any unfinished task may carry `check_after`. `sweep()` finds the reminders
whose time has come and hands each owner one ordinary message through whatever
transport the team uses -- the same mechanism a teammate's message rides, so a
reminder wakes an agent on any host that can wake an agent for mail.

What sweep() cannot do is call itself. `Supervisor.step()` calls it on every
pass, which covers a team run with `--daemon`. A host that schedules agents
some other way -- a push transport, agents driven directly in a CLI, a
`--say` run that went idle and exited -- must call it too, and
`python -m ateam.tasks sweep` exists so that any scheduler it already has
can. A reminder nobody sweeps is silent, which is the failure this feature was
built to remove, so it is made loud in three places: `run_until_idle` says how
many reminders it is leaving behind, `ateam.doctor` warns when reminders are
overdue, and the tool result that sets one says what delivers it.

## Reminders that cannot fire are refused, not stored

The first version stored `check_after` verbatim and compared strings. "1 hour"
fired at once; "2H" and a timestamp without zero-padding waited until the next
year; a time without a zone was quietly read as UTC. Each of those is a
reminder that looks set and is not. Now a reminder is parsed when it is set --
a duration, or an ISO-8601 time that names its zone -- and stored normalised,
or the call fails with a sentence saying what would have worked. A value that
still cannot be read (written by an older version, or by hand) fires at the
next sweep rather than never: early and noisy beats silent.

## Statuses a board needs

`icebox` is parked work: nobody picks it up, its reminders wait, and only
the operator takes it back out. `review` is work that is done and waiting on
an approved review; complete_task still needs that review where the task
requires one. A task may name the `channel` its discussion happens in --
the link is stored on the task only, and a channel's tasks are the ones that
name it -- and carries a `rank` for its order within a column. A change of
status on a linked task is noted in its channel, quietly.

A linked task also has a `thread`: the message its discussion hangs off.
An agent's task is linked to the thread of the message it was answering
(see ateam.mcp_tasks); otherwise the first notice about the task starts
one, and every later notice is a reply in it. `origin` is the message the
task came from, a direct message included, so "where did this come from?"
has an answer on the task.

The operator ("user") may change any task, owned or not: that is what moving
a card on a board is.

## Work that has to wait for other work

`blocked_by` names the tasks that must be done first. A task with one still
open cannot be started, except by the operator, and whoever owns it hears when
the last one closes, or when one closes without being done. Before this, the
order lived in a note, and an agent told to wait for another's result had to
remember to come back.

`writes` names the paths a task expects to change. It locks nothing: agents
share a checkout, and most writes go through a shell no list could police. What
it does is say so, on the task, when two open tasks expect to touch one path,
while that is still cheap to sort out.

## Why no separate project file

A project is a grouping string on a task, not a second record with its own
lifecycle. Two append-only logs that must agree with each other is a
consistency problem this does not need to have.
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import duties, filelock

FILENAME = "tasks.jsonl"

STATUSES = {"icebox", "queued", "claimed", "running", "blocked", "review",
            "done", "failed", "cancelled"}
TERMINAL = {"done", "failed", "cancelled"}
#: A task in one of these has begun, so what it waits for must be finished.
STARTED = {"claimed", "running"}
#: Not finished, but nothing happens to it: no claims, no reminders.
PAUSED = {"icebox"}
OPERATOR = "user"

_KIND_CREATED = "created"
_KIND_UPDATED = "updated"
_KIND_SWEPT = "swept"

# Fields an "updated" or "swept" entry may carry into the folded state. `id`,
# `kind`, `ts` and `by` describe the log entry, not the task.
_MUTABLE_FIELDS = ("project", "title", "owner", "status", "note", "check_after",
                   "collaborators", "requires_review", "evidence", "until",
                   "poll_every", "deadline", "channel", "rank", "links",
                   "parent", "level", "priority", "auto_close", "archived", "archived_by",
                   "blocked_by", "writes", "thread",
                   # a standing duty (ateam.duties): its schedule, the run out
                   # with its owner, and what became of the runs before
                   "every", "run_due", "last_done", "last_note", "missed",
                   "missed_total", "last_missed")

# What an agent may call a status and what it is here. The allowed set was
# discoverable only by trial -- update_task(status="in_progress") was refused
# with a list -- so the obvious spellings are accepted and normalised.
STATUS_ALIASES = {"in_progress": "running", "in-progress": "running",
                  "started": "running", "active": "running",
                  "waiting": "blocked", "paused": "blocked",
                  "complete": "done", "completed": "done", "finished": "done",
                  "closed": "done", "canceled": "cancelled", "todo": "queued",
                  "open": "queued", "pending": "queued",
                  "backlog": "icebox", "parked": "icebox", "someday": "icebox",
                  "ice": "icebox", "iced": "icebox",
                  "in_review": "review", "in-review": "review",
                  "awaiting_review": "review", "needs_review": "review",
                  "reviewing": "review"}


def normalize_status(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return STATUS_ALIASES.get(s, s)

_FMT = "%Y-%m-%dT%H:%M:%SZ"
_CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$", re.IGNORECASE)
_UNIT_SECONDS = {}
for _names, _secs in ((("s", "sec", "secs", "second", "seconds"), 1),
                      (("m", "min", "mins", "minute", "minutes"), 60),
                      (("h", "hr", "hrs", "hour", "hours"), 3600),
                      (("d", "day", "days"), 86400),
                      (("w", "wk", "wks", "week", "weeks"), 604800)):
    _UNIT_SECONDS.update(dict.fromkeys(_names, _secs))

WHEN_HELP = ("a duration like '90m', '2h', '1.5 hours' or '1d', or an "
             "ISO-8601 time that names its zone, like '2026-09-26T05:00:00Z' "
             "or '2026-09-26T05:00:00-07:00'")


class TaskError(ValueError):
    """A refusal the caller can act on, phrased as a sentence."""


def path(team_dir: Path | str) -> Path:
    return Path(team_dir) / FILENAME


def _now() -> str:
    return time.strftime(_FMT, time.gmtime())


def parse_when(raw: str, now: float | None = None) -> str:
    """A reminder time, normalised to UTC `YYYY-MM-DDTHH:MM:SSZ`, or TaskError.

    Durations are relative to `now` (epoch seconds, default the present).
    A timestamp without a zone is refused rather than guessed: "05:00" means
    something different on every host that reads it, and whichever guess is
    wrong moves the reminder by hours without anyone noticing.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TaskError(f"check_after is empty; give {WHEN_HELP}")
    s = raw.strip()
    m = _DURATION.match(s)
    if m and m.group(2).lower() in _UNIT_SECONDS:
        secs = float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
        base = time.time() if now is None else now
        return time.strftime(_FMT, time.gmtime(base + secs))
    iso = s[:-1] + "+00:00" if s[-1:] in ("z", "Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise TaskError(f"check_after {raw!r} is not a time this can read; "
                        f"give {WHEN_HELP}") from None
    if dt.tzinfo is None:
        raise TaskError(f"check_after {raw!r} has no timezone, so it would fire "
                        f"at a different moment on every host; add 'Z' for UTC "
                        f"or an offset like '-07:00'")
    return dt.astimezone(timezone.utc).strftime(_FMT)


def parse_duration(raw: str) -> str:
    """A duration shorthand, validated and returned as written ('90m')."""
    if not isinstance(raw, str) or not raw.strip():
        raise TaskError("poll_every is empty; give a duration like '10m' or '1h'")
    m = _DURATION.match(raw.strip())
    if not m or m.group(2).lower() not in _UNIT_SECONDS:
        raise TaskError(f"poll_every {raw!r} is not a duration; give e.g. '10m' or '1h'")
    return raw.strip()


def duration_seconds(raw: str) -> float:
    m = _DURATION.match(raw.strip())
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


# --- waiting on something external ------------------------------------------
#
# Twenty wakes in seventy minutes to learn "still running" twenty times.
# A task may instead carry a condition the sweep checks itself, in-process,
# and the owner is woken once: when it holds, or when the deadline passes.
# Conditions read files and process tables; none runs a command, because
# the sweep runs with the supervisor's privileges and a command an agent
# wrote would run with them too.

UNTIL_KINDS = ("file_exists", "file_contains", "pid_exited")


def parse_until(raw: str) -> dict:
    """'file_exists:/path', 'file_contains:/path:text', 'pid_exited:1234'."""
    kind, _, rest = (raw or "").strip().partition(":")
    kind = kind.strip().lower()
    if kind == "file_exists" and rest:
        return {"type": kind, "path": rest.strip()}
    if kind == "file_contains" and rest:
        path, _, text = rest.partition(":")
        if path.strip() and text:
            return {"type": kind, "path": path.strip(), "text": text}
    if kind == "pid_exited" and rest.strip().isdigit():
        return {"type": kind, "pid": int(rest.strip())}
    raise TaskError(f"until {raw!r} is not a condition this can check; give "
                    f"'file_exists:/path', 'file_contains:/path:text' or "
                    f"'pid_exited:1234'")


def validate_until(until: dict) -> None:
    if not isinstance(until, dict) or until.get("type") not in UNTIL_KINDS:
        raise TaskError(f"until must be one of {', '.join(UNTIL_KINDS)}")


def satisfied(until: dict) -> bool:
    """Does the condition hold right now? Any error reads as 'not yet'."""
    try:
        t = until.get("type")
        if t == "file_exists":
            return Path(until["path"]).expanduser().exists()
        if t == "file_contains":
            return until["text"] in Path(until["path"]).expanduser().read_text(
                encoding="utf-8", errors="replace")
        if t == "pid_exited":
            try:
                os.kill(int(until["pid"]), 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False


# --- the log --------------------------------------------------------------

def _append(team_dir: Path | str, entry: dict) -> None:
    p = path(team_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read(team_dir: Path | str) -> list[dict]:
    """Every log entry in file order. Unparseable lines are skipped, not fatal --
    a task log a person can still read after a bad line is more useful than one
    that refuses to load. `ateam.doctor` reports them."""
    try:
        text = path(team_dir).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def snapshot(team_dir: Path | str) -> dict[str, dict]:
    """Current state per task id, folded from the log in order.

    This is the whole state. A process that just started, reading a log
    written by one that no longer exists, arrives at the same answer a process
    that had been running the entire time would have -- which is what
    "return to a project after a power cycle" means here.
    """
    state: dict[str, dict] = {}
    for rec in read(team_dir):
        tid = rec["id"]
        if rec.get("kind", _KIND_CREATED) == _KIND_CREATED:
            state[tid] = {k: rec.get(k) for k in
                          ("id", "project", "title", "owner", "status",
                           "created_by", "note", "check_after", "collaborators",
                           "requires_review", "evidence", "until", "poll_every",
                           "deadline", "channel", "rank", "links",
                           "parent", "level", "priority", "auto_close", "archived",
                           "blocked_by", "writes", "thread", "origin", "every")}
            state[tid]["created_at"] = rec.get("ts")
            state[tid]["updated_at"] = rec.get("ts")
        elif tid in state:
            # A mutation naming a task never created is dropped, not invented.
            for k in _MUTABLE_FIELDS:
                if k in rec:
                    state[tid][k] = rec[k]
            state[tid]["updated_at"] = rec.get("ts")
    return state


def get(team_dir: Path | str, task_id: str) -> dict | None:
    return snapshot(team_dir).get(task_id)


# --- mutations: each one decides and writes under the same lock -------------

def create(team_dir: Path | str, project: str, title: str,
           owner: str | None = None, created_by: str = "",
           note: str = "", check_after: str | None = None,
           collaborators: list[str] | None = None, requires_review: bool = False,
           until: dict | None = None, poll_every: str | None = None,
           deadline: str | None = None, channel: str | None = None,
           status: str | None = None, rank: float | None = None,
           parent: str | None = None, level: int | None = None,
           priority: int | None = None, auto_close: bool = False,
           blocked_by: list[str] | None = None, writes: list[str] | None = None,
           thread: str | None = None, origin: dict | None = None,
           every: str | None = None) -> dict:
    """Append a new task and return its current state. `status` may start
    it in the icebox; anything else starts queued. `thread` is the message
    in `channel` its discussion hangs off; `origin` the message it came
    from (see _origin).

    `parent` puts it under another task (see "the tree" below); a task with
    a parent is in the parent's project, whether or not it names one."""
    parent = (parent or "").strip() or None
    if parent:
        up = get(team_dir, parent)
        if up is None:
            raise TaskError(f"no task {parent!r} to put this under")
        project = _inherit(up, project)
    if not title or not title.strip():
        raise TaskError("title is required")
    if until:
        validate_until(until)
    status = normalize_status(status) or "queued"
    if status not in ("queued", "icebox"):
        raise TaskError(f"a new task starts queued or in the icebox, not {status!r}")
    channel = _check_channel(team_dir, channel)
    every = _every(every) if every else None
    if every:
        owner = owner or (created_by if created_by and created_by != OPERATOR else None)
        if not owner:
            raise TaskError("a standing duty needs an owner to run it; give owner=")
    entry = {
        "id": "task_" + uuid.uuid4().hex[:8],
        "kind": _KIND_CREATED,
        "ts": _now(),
        "project": (project or "").strip(),
        "title": title.strip(),
        "owner": owner,
        "status": status,
        "created_by": created_by,
        "note": note.strip() if note else "",
        "check_after": parse_when(check_after) if check_after else None,
        "collaborators": [c for c in (collaborators or []) if c and c != owner],
        "requires_review": bool(requires_review),
        "evidence": None,
        "until": until or None,
        "poll_every": parse_duration(poll_every) if poll_every else None,
        "deadline": parse_when(deadline) if deadline else None,
        "channel": channel,
        "rank": _rank(rank),
        "parent": parent,
        "level": _level(level),
        "priority": _priority(priority),
        "auto_close": bool(auto_close),
        "blocked_by": _clean(blocked_by),
        "writes": _paths(writes),
        "thread": (str(thread).strip() or None) if channel and thread else None,
        "origin": _origin(origin),
        "every": every,
    }
    if every and not entry["check_after"]:
        entry["check_after"] = duties.first_run(every)
    if until and not entry["check_after"]:
        # A condition is checked from the next sweep on, not left to wait
        # for a reminder nobody set.
        entry["check_after"] = _now()
    with filelock.locked(path(team_dir)):
        _check_blockers(snapshot(team_dir), entry["id"], entry["blocked_by"])
        _append(team_dir, entry)
    t = get(team_dir, entry["id"])
    if channel:
        _notice(team_dir, t, created_by or OPERATOR, "created")
        t = get(team_dir, entry["id"])
    return t


def claim(team_dir: Path | str, task_id: str, agent: str) -> dict | None:
    """Take ownership. None if no such task; TaskError if someone else owns it.

    The check and the write happen under one lock, so of any number of agents
    claiming at once, exactly one is told it won.
    """
    with filelock.locked(path(team_dir)):
        current = snapshot(team_dir).get(task_id)
        if current is None:
            return None
        if current.get("owner") and current["owner"] != agent:
            raise TaskError(f"task {task_id} is already owned by "
                            f"'{current['owner']}'; they can hand it to you "
                            f"with update_task(owner='{agent}')")
        if current.get("status") in PAUSED and agent != OPERATOR:
            raise TaskError(f"task {task_id} is in the icebox: the operator has "
                            f"parked it. Ask them before working on it")
        if agent != OPERATOR:
            _refuse_if_waiting(snapshot(team_dir), current)
        _append(team_dir, {"id": task_id, "kind": _KIND_UPDATED, "ts": _now(),
                           "by": agent, "owner": agent, "status": "claimed"})
    return get(team_dir, task_id)


def update(team_dir: Path | str, task_id: str, by: str, *,
           status: str | None = None, owner: str | None = None,
           check_after: str | None = None, clear_check_after: bool = False,
           note: str | None = None, evidence: str | None = None,
           collaborators: list[str] | None = None,
           until: dict | None = None, poll_every: str | None = None,
           deadline: str | None = None, channel: str | None = None,
           rank: float | None = None, title: str | None = None,
           project: str | None = None, links: list | None = None,
           parent: str | None = None, level: int | str | None = None,
           priority: int | str | None = None, auto_close: bool | None = None,
           archived: bool | None = None, blocked_by: list[str] | None = None,
           writes: list[str] | None = None, thread: str | None = None,
           every: str | None = None) -> dict | None:
    """Append a mutation by `by`. None if task_id is unknown.

    Only the owner -- or the operator -- may change an owned task, and
    passing `owner` is how they hand it to someone else. `channel=""`
    unlinks the task from its channel. `check_after=None` leaves any reminder alone;
    `clear_check_after=True` removes it. Finishing a task removes its reminder,
    because a reminder on finished work would only ever wake someone to read
    that it was finished.
    """
    status = normalize_status(status)
    if status is not None and status not in STATUSES:
        raise TaskError(f"unknown status {status!r}; use one of "
                        f"{', '.join(sorted(STATUSES))}")
    when = parse_when(check_after) if check_after is not None else None
    if until:
        validate_until(until)
    poll = parse_duration(poll_every) if poll_every else None
    schedule = (_every(every) if every.strip() else "") if every is not None else None
    due_by = parse_when(deadline) if deadline else None
    if channel is not None:
        channel = _check_channel(team_dir, channel) if channel.strip() else ""
    if title is not None and not title.strip():
        raise TaskError("a task's title cannot be empty")
    if archived is not None and by != OPERATOR:
        raise TaskError("archiving is the operator's: it takes the task and "
                        "everything under it out of view")
    with filelock.locked(path(team_dir)):
        state = snapshot(team_dir)
        current = state.get(task_id)
        if current is None:
            return None
        if parent is not None and parent.strip():
            _check_parent(state, task_id, parent.strip())
        # A subtask is in its parent's project: moved under a task, it takes
        # that task's; its project changed, so does everything under it.
        new_parent = (parent.strip() or None) if parent is not None else current.get("parent")
        if (parent is not None or project is not None) and new_parent in state:
            inherited = (_inherit(state[new_parent], project) if project is not None
                         else (state[new_parent].get("project") or "").strip()
                         or (current.get("project") or ""))
            project = inherited if inherited != (current.get("project") or "") else project
        if current.get("owner") and current["owner"] != by and by != OPERATOR:
            raise TaskError(f"task {task_id} is owned by '{current['owner']}', "
                            f"not '{by}'")
        if (current.get("status") in PAUSED and status is not None
                and status not in PAUSED and by != OPERATOR):
            raise TaskError(f"task {task_id} is in the icebox: the operator "
                            f"parked it, and only they take it back out")
        waiting_on = _clean(blocked_by) if blocked_by is not None else None
        if waiting_on is not None:
            _check_blockers(state, task_id, waiting_on)
        if status in STARTED and by != OPERATOR:
            _refuse_if_waiting(state, current if waiting_on is None
                               else {**current, "blocked_by": waiting_on})
        final_status = status or current.get("status")
        final_channel = channel if channel is not None else current.get("channel")
        if when and final_status in TERMINAL:
            raise TaskError(f"task {task_id} is {final_status}; a reminder on "
                            f"it would never fire")
        # Closing a duty when a pass was done is what ended duties before:
        # the run is recorded by complete_task, and the duty is ended on
        # purpose, by the operator or with every="" in the same call.
        if (current.get("every") and status in TERMINAL and schedule != ""
                and by != OPERATOR):
            raise TaskError(f"task {task_id} is a standing duty (every "
                            f"{current['every']}): it does not finish. To record this "
                            f"run, complete_task({task_id!r}, note=...); the duty "
                            f"stays open for the next. To end the duty itself, ask "
                            f"the operator")
        entry = {"id": task_id, "kind": _KIND_UPDATED, "ts": _now(), "by": by}
        if status is not None:
            entry["status"] = status
        if owner is not None:
            entry["owner"] = owner
        if final_status in TERMINAL or clear_check_after:
            entry["check_after"] = None
        elif when:
            entry["check_after"] = when
        if schedule == "" and current.get("every"):
            entry.update(every=None, run_due=None)
            if not when:
                entry["check_after"] = None
        elif schedule and schedule != current.get("every"):
            if not current.get("owner") and owner is None:
                raise TaskError("a standing duty needs an owner to run it; give owner=")
            entry["every"] = schedule
            if not when and final_status not in TERMINAL:
                entry["check_after"] = duties.first_run(schedule)
        if note is not None:
            entry["note"] = note.strip()
        if evidence is not None:
            entry["evidence"] = evidence.strip()
        if collaborators is not None:
            entry["collaborators"] = [c for c in collaborators if c]
        if until:
            entry["until"] = until
            if not when and not current.get("check_after"):
                entry["check_after"] = _now()
        if poll:
            entry["poll_every"] = poll
        if due_by:
            entry["deadline"] = due_by
        if channel is not None and channel != (current.get("channel") or ""):
            entry["channel"] = channel or None
            entry["thread"] = ((thread or "").strip() or None) if channel else None
        elif thread is not None and final_channel:
            entry["thread"] = thread.strip() or None
        if rank is not None:
            entry["rank"] = _rank(rank)
        if title is not None:
            entry["title"] = title.strip()
        if project is not None:
            entry["project"] = project.strip()
        if links is not None:
            entry["links"] = _links(links)
        if parent is not None:
            entry["parent"] = parent.strip() or None
        if level is not None:
            entry["level"] = None if str(level).strip() in ("", "auto") else _level(level)
        if priority is not None:
            entry["priority"] = None if str(priority).strip() == "" else _priority(priority)
        if auto_close is not None:
            entry["auto_close"] = bool(auto_close)
        if archived is not None:
            entry["archived"] = _now() if archived else None
            entry["archived_by"] = by if archived else None
        if waiting_on is not None:
            entry["blocked_by"] = waiting_on
        if writes is not None:
            entry["writes"] = _paths(writes)
        _append(team_dir, entry)
        old = (current.get("project") or "").strip()
        if project is not None and project.strip() != old:
            for tid in _subtree(state, task_id):
                if (state[tid].get("project") or "").strip() == old:
                    _append(team_dir, {"id": tid, "kind": _KIND_UPDATED, "ts": _now(),
                                       "by": by, "project": project.strip()})
    t = get(team_dir, task_id)
    if t and status in TERMINAL and current.get("status") not in TERMINAL:
        _children_closed(team_dir, t)
        _blockers_closed(team_dir, t)
    if t and t.get("channel"):
        if channel and channel != current.get("channel"):
            _notice(team_dir, t, by, "linked to this channel")
        elif status is not None and status != current.get("status"):
            _notice(team_dir, t, by, f"{current.get('status')} \u2192 {status}")
    return t


def _clean(names) -> list[str]:
    """Distinct, in order."""
    return list(dict.fromkeys(n.strip() for n in names or [] if n and n.strip()))


def _paths(paths) -> list[str]:
    """`writes` in one spelling: no ./ in front and no / behind."""
    out = (p.strip().removeprefix("./").rstrip("/") for p in paths or [] if p and p.strip())
    return list(dict.fromkeys(p or "." for p in out))


def _check_blockers(state: dict, task_id: str, blockers: list[str]) -> None:
    for b in blockers:
        if b == task_id:
            raise TaskError(f"task {task_id} cannot wait for itself")
        if b not in state:
            raise TaskError(f"no task {b!r} to wait for")
        stack, seen = [b], set()
        while stack:
            cur = stack.pop()
            if cur == task_id:
                raise TaskError(f"{b} already waits for {task_id}, so {task_id} "
                                f"waiting for it would stop both")
            if cur not in seen:
                seen.add(cur)
                stack.extend((state.get(cur) or {}).get("blocked_by") or [])


def open_blockers(state: dict, t: dict) -> list[str]:
    """What `t` still waits for. A blocker that failed or was cancelled still
    counts: the work it was meant to deliver does not exist."""
    return [b for b in t.get("blocked_by") or []
            if (state.get(b) or {}).get("status") != "done"]


def _refuse_if_waiting(state: dict, t: dict) -> None:
    waiting = open_blockers(state, t)
    if waiting:
        said = ", ".join(f"{b} ({(state.get(b) or {}).get('status')})" for b in waiting)
        raise TaskError(f"task {t['id']} waits for {said}. You will be told when "
                        f"it is ready, or take the wait off with "
                        f"update_task(blocked_by='none')")


def overlaps(state: dict, t: dict) -> list[dict]:
    """Other open tasks that expect to write where `t` does."""
    def same(a: str, b: str) -> bool:
        return a == b or "." in (a, b) or a.startswith(b + "/") or b.startswith(a + "/")

    if t.get("status") in TERMINAL or not t.get("writes"):
        return []
    return [o for o in state.values()
            if o["id"] != t["id"] and o.get("status") not in TERMINAL
            and any(same(a, b) for a in t["writes"] for b in o.get("writes") or [])]


def _blockers_closed(team_dir, t: dict) -> None:
    """A task closed: tell the owner of each task that was waiting for it."""
    state = snapshot(team_dir)
    for x in state.values():
        if t["id"] not in (x.get("blocked_by") or []) or x.get("status") in TERMINAL:
            continue
        to = x.get("owner") or x.get("created_by") or OPERATOR
        name = f"{x['id']} \u201c{x.get('title')}\u201d"
        if t.get("status") != "done":
            _message(team_dir, to, f"{t['id']} \u201c{t.get('title')}\u201d, which {name} "
                                   f"waits for, closed as {t.get('status')}. Decide what "
                                   f"happens to {x['id']}: wait for a replacement, or "
                                   f"change or close it.", x["id"])
        elif not open_blockers(state, x):
            _message(team_dir, to, f"{name} is ready: everything it was waiting for is "
                                   f"done, the last being {t['id']} "
                                   f"\u201c{t.get('title')}\u201d.", x["id"])


def _check_channel(team_dir, channel: str | None) -> str | None:
    """A channel a task can be linked to, or a TaskError naming the choices."""
    if channel is None or not str(channel).strip():
        return None
    from . import channels as channels_lib
    name = str(channel).strip().lstrip("#").lower()
    live = channels_lib.active(team_dir)
    if name not in live:
        raise TaskError(f"no open channel #{name}; channels: "
                        f"{', '.join('#' + c for c in sorted(live)) or 'none'}")
    return name


def _links(links) -> list[dict]:
    """[{title, url}]: bookmarks on a task -- a URL, a path, anything worth
    keeping beside it."""
    out = []
    for x in links or []:
        url = str((x.get("url") if isinstance(x, dict) else x) or "").strip()
        if not url:
            continue
        title = str((x.get("title") if isinstance(x, dict) else "") or "").strip() or url
        out.append({"title": title[:200], "url": url[:2000]})
    return out[:50]


def _level(level) -> int | None:
    if level is None:
        return None
    try:
        n = int(level)
    except (TypeError, ValueError):
        raise TaskError(f"level is a whole number (0 a task, 1 a project, ...), "
                        f"not {level!r}") from None
    if not 0 <= n <= 9:
        raise TaskError("level is between 0 and 9")
    return n


def _priority(priority) -> int | None:
    if priority is None:
        return None
    raw = str(priority).strip().lower().lstrip("p")
    try:
        n = int(raw)
    except ValueError:
        raise TaskError(f"priority is P0 (highest) to P4, not {priority!r}") from None
    if not 0 <= n <= 4:
        raise TaskError("priority is P0 (highest) to P4")
    return n


def _rank(rank) -> float | None:
    if rank is None:
        return None
    try:
        return float(rank)
    except (TypeError, ValueError):
        raise TaskError(f"rank is a number, not {rank!r}") from None


def _notice(team_dir, t: dict, by: str, what: str) -> None:
    """A line about `t` in its channel, as a reply in its thread. A task
    with no thread yet gets this line as one, so its discussion has a
    place to hang off."""
    from . import channels as channels_lib
    from . import roster as roster_lib
    who = by
    if by == OPERATOR:
        who = roster_lib.operator(channels_lib._roster_doc(team_dir)).get("name") or "the operator"
    posted = channels_lib.notice(team_dir, t["channel"], by,
                                 f"Task {t['id']} \u201c{t.get('title')}\u201d: {what} "
                                 f"({who}; owner {t.get('owner') or 'nobody'})",
                                 task_id=t["id"], thread=t.get("thread"))
    if posted and not t.get("thread"):
        with filelock.locked(path(team_dir)):
            _append(team_dir, {"id": t["id"], "kind": _KIND_UPDATED, "ts": _now(),
                               "by": by, "thread": posted})


def _origin(origin) -> dict | None:
    """{id, channel?, thread?, from?, to?}: the message a task came from,
    kept as given, ids and names only."""
    if not isinstance(origin, dict) or not origin.get("id"):
        return None
    return {k: str(origin[k])[:200] for k in ("id", "channel", "thread", "from", "to")
            if origin.get(k)}


def _every(raw) -> str:
    try:
        return duties.parse(raw)
    except duties.DutyError as e:
        raise TaskError(str(e)) from None


def record_run(team_dir: Path | str, task_id: str, by: str, note: str,
               evidence: str | None = None) -> dict | None:
    """One run of a standing duty, done: on its record, and the duty stays
    open. The run counted is the one out with its owner; done between runs,
    it is an extra pass and still counts."""
    with filelock.locked(path(team_dir)):
        current = snapshot(team_dir).get(task_id)
        if current is None:
            return None
        entry = {"id": task_id, "kind": _KIND_UPDATED, "ts": _now(), "by": by,
                 "run": "done", "run_for": current.get("run_due"),
                 "last_done": _now(), "last_note": note.strip()[:2000],
                 "run_due": None, "missed": 0}
        if evidence is not None:
            entry["evidence"] = evidence.strip()
        if current.get("status") in ("queued", "claimed"):
            entry["status"] = "running"
        _append(team_dir, entry)
    return get(team_dir, task_id)


def complete(team_dir: Path | str, task_id: str, by: str, note: str,
             evidence: str | None = None) -> dict | None:
    """Close a task, with the closing note it must carry.

    Three closed tasks still read "actively triaging..." because closing did
    not ask for a note and the fold kept the old one. A joint task was closed
    by one owner ten minutes before the other's result landed. A deliverable
    reached the user before its review existed. So: a note, every
    collaborator seen working since the task was created, and -- where the
    task says so -- an approved review that names it.
    """
    if not note or not note.strip():
        raise TaskError("a closing note is required: what was done and where "
                        "the result is")
    current = get(team_dir, task_id)
    if current is None:
        return None
    if current.get("owner") and current["owner"] != by:
        raise TaskError(f"task {task_id} is owned by '{current['owner']}', not '{by}'")
    if current.get("every") and current.get("status") not in TERMINAL:
        return record_run(team_dir, task_id, by, note, evidence)
    from .activity import author_activity
    idle = []
    for c in current.get("collaborators") or []:
        verifiable, evidence_of = author_activity(Path(team_dir), c, current.get("created_at") or "")
        if verifiable and not evidence_of:
            idle.append(c)
    if idle:
        raise TaskError(f"task {task_id} lists {', '.join(idle)} as collaborator(s) "
                        f"with no recorded activity since it was created; their "
                        f"part has not landed. Wait for it, or remove them with "
                        f"update_task(collaborators=...)")
    if current.get("requires_review") and not approved_review_for(team_dir, task_id):
        raise TaskError(f"task {task_id} requires an approved review before it "
                        f"closes, and none names it. Have a teammate "
                        f"record_review(..., task_id='{task_id}') first")
    return update(team_dir, task_id, by, status="done", note=note, evidence=evidence)


def approved_review_for(team_dir: Path | str, task_id: str) -> dict | None:
    """The most recent approved work review naming this task, or the
    operator's waiver of one, or None."""
    from .activity import jsonl
    found = None
    for r in jsonl(Path(team_dir) / "reviews.jsonl"):
        if r.get("task_id") != task_id:
            continue
        if (r.get("verdict") == "approved" and r.get("kind", "work") == "work") \
                or r.get("kind") == "waiver":
            found = r
    return found


def waive_review(team_dir: Path | str, task_id: str, by: str = "user",
                 reason: str = "") -> dict:
    """The operator's "ship it without review", on the record.

    Written as its own kind in reviews.jsonl, so it satisfies the
    requires_review gate without ever being counted as a review: the report
    and the retrospective read kind == "work" and never see it. It is a
    command and not a tool argument so that no agent is offered it.

    How far "only the operator" holds depends on the runtime. Where agents
    are contained they cannot run this or write the team directory. Where
    they have an unconfined shell (the agy CLI), nothing can authenticate the
    caller: this refuses a waiver signed by a teammate and one issued from
    inside an agent's session (the runners export ATEAM_AGENT to everything
    an agent starts), which stops an agent that reaches for the command and
    not one that sets out to forge it. Add the command to
    policy.forbidden_commands where a pre-tool hook runs.
    """
    current = get(team_dir, task_id)
    if current is None:
        raise TaskError(f"no task with id '{task_id}'")
    inside = os.environ.get("ATEAM_AGENT", "").strip()
    if inside:
        raise TaskError(f"a waiver is the operator's, and this is {inside}'s "
                        f"session; ask the operator to waive it")
    try:
        doc = json.loads((Path(team_dir) / "roster.json").read_text(encoding="utf-8"))
        teammates = {a.get("name") for a in doc.get("agents", []) if isinstance(a, dict)}
    except (OSError, ValueError, AttributeError):
        teammates = set()
    teammates.add(current.get("owner"))
    if not by or not by.strip() or by.strip() in teammates:
        raise TaskError("a waiver is the operator's, not a teammate's; pass "
                        "--by with who is waiving")
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "waiver",
             "verdict": "waived", "reviewer": by.strip(), "author": current.get("owner"),
             "task_id": task_id, "what": current.get("title"),
             "findings": reason.strip() or "review waived by the operator"}
    path_ = Path(team_dir) / "reviews.jsonl"
    with filelock.locked(path_):
        with path_.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


# --- the tree ---------------------------------------------------------------
#
# A task may have a parent: the larger piece of work it serves. Projects,
# programs, initiatives are not separate things -- each is a task with
# children, and the structure is whatever the work grows into. One parent
# each and no cycles, so every roll-up below is well defined and archiving
# a branch hides exactly that branch.
#
# A task's level says how big it is: 0 a task, 1 a project, 2 a program...
# (the names are the team's: roster "task_levels"). Set it, and it stays;
# leave it, and it is worked out: 0 with no children, else one above the
# highest child. A project set at level 1 is one before it has any tasks,
# and a layer an agent adds underneath does not promote it.

DEFAULT_LEVEL_NAMES = ["task", "project", "program", "portfolio"]


def level_names(team_dir) -> list[str]:
    try:
        doc = json.loads((Path(team_dir) / "roster.json").read_text(encoding="utf-8"))
        names = doc.get("task_levels")
        if isinstance(names, list) and names and all(isinstance(n, str) and n.strip() for n in names):
            return [n.strip().lower() for n in names]
    except (OSError, ValueError):
        pass
    return list(DEFAULT_LEVEL_NAMES)


def _inherit(up: dict, project: str | None) -> str:
    """A subtask's project: its parent's. Naming another is an error, not a
    silent choice -- a project is how work is scoped (roster
    allowed_task_projects), and a subtree split across two would be in
    neither."""
    theirs = (up.get("project") or "").strip()
    mine = (project or "").strip()
    if mine and theirs and mine != theirs:
        raise TaskError(f"a subtask is in its parent's project, {theirs!r}, not "
                        f"{mine!r}: leave project out, or put it under a task in {mine!r}")
    return theirs or mine


def _subtree(state: dict, task_id: str) -> list[str]:
    """Every task under `task_id`, at any depth."""
    kids: dict[str, list[str]] = {}
    for tid, t in state.items():
        if t.get("parent"):
            kids.setdefault(t["parent"], []).append(tid)
    out, todo = [], list(kids.get(task_id, []))
    while todo:
        tid = todo.pop()
        if tid not in out and tid != task_id:
            out.append(tid)
            todo += kids.get(tid, [])
    return out


def _check_parent(state: dict, task_id: str, parent: str) -> None:
    if parent not in state:
        raise TaskError(f"no task {parent!r} to put {task_id} under")
    seen, cur = set(), parent
    while cur:
        if cur == task_id:
            raise TaskError(f"{parent} is under {task_id} already; a task cannot "
                            f"be inside itself")
        if cur in seen:
            break
        seen.add(cur)
        cur = (state.get(cur) or {}).get("parent")


def tree(team_dir: Path | str | None = None, state: dict | None = None) -> dict[str, dict]:
    """{id: {children, level, depth, path, open, done, total, archived_by}}.

    path: the titles from the top down to (not including) this task.
    total/done: the tasks under it, at any depth, and how many are closed.
    archived_by: the id of the archived task this one is under (itself
    included), or None.
    """
    state = state if state is not None else snapshot(team_dir)
    kids: dict[str, list[str]] = {}
    for tid, t in state.items():
        p = t.get("parent")
        if p and p in state and p != tid:
            kids.setdefault(p, []).append(tid)
    out: dict[str, dict] = {}

    def level(tid, stack=()):
        if tid in out and "level" in out[tid]:
            return out[tid]["level"]
        t = state[tid]
        if isinstance(t.get("level"), int):
            n = t["level"]
        elif tid in stack:                     # a cycle in an old log: stop
            n = 0
        else:
            n = max((level(c, stack + (tid,)) for c in kids.get(tid, [])), default=-1) + 1
        out.setdefault(tid, {})["level"] = n
        return n

    def under(tid, stack=()):
        done = total = 0
        for c in kids.get(tid, []):
            if c in stack:
                continue
            total += 1
            done += state[c].get("status") in TERMINAL
            d, n = under(c, stack + (tid,))
            done, total = done + d, total + n
        return done, total

    for tid in state:
        level(tid)
        path, arch, seen, cur = [], None, {tid}, tid
        if state[tid].get("archived"):
            arch = tid
        p = state[tid].get("parent")
        while p and p in state and p not in seen:
            seen.add(p)
            path.insert(0, state[p].get("title") or p)
            if state[p].get("archived") and not arch:
                arch = p
            p = state[p].get("parent")
        done, total = under(tid)
        out[tid].update({"children": kids.get(tid, []), "depth": len(path), "path": path,
                         "done": done, "total": total, "archived_by": arch})
    return out


def _message(team_dir, to: str, text: str, task_id: str, kind: str = "reminder") -> None:
    """A message from the supervisor about a task. Never raises: a nudge
    must not undo the change that caused it."""
    try:
        from . import transport as transport_lib
        cfg = json.loads(os.environ.get("ATEAM_BUS_CONFIG") or "{}")
        cfg.setdefault("team_dir", str(team_dir))
        bus = transport_lib.load("supervisor", config=cfg)
        try:
            try:
                bus.send(to, text, kind=kind, task_id=task_id)
            except TypeError:
                bus.send_kind(to, text, kind=kind, task_id=task_id)
        finally:
            if hasattr(bus, "close"):
                bus.close()
    except Exception:
        pass


def _children_closed(team_dir, t: dict) -> None:
    """A task just closed: if that closed its parent's last open child, close
    the parent (auto_close) or tell its owner (the operator, if nobody owns
    it) once. Recurses upward through auto-closing parents."""
    pid = t.get("parent")
    if not pid:
        return
    state = snapshot(team_dir)
    parent = state.get(pid)
    if not parent or parent.get("status") in TERMINAL:
        return
    siblings = [x for x in state.values() if x.get("parent") == pid]
    if not siblings or any(x.get("status") not in TERMINAL for x in siblings):
        return
    failed = [x for x in siblings if x.get("status") != "done"]
    if parent.get("auto_close") and not failed:
        with filelock.locked(path(team_dir)):
            _append(team_dir, {"id": pid, "kind": _KIND_UPDATED, "ts": _now(), "by": "auto",
                               "status": "done", "check_after": None,
                               "closed_because": "every task under it is done"})
        closed = get(team_dir, pid)
        if closed and closed.get("channel"):
            _notice(team_dir, closed, "auto", "done: every task under it is done")
        if closed:
            _children_closed(team_dir, closed)
        return
    owner = parent.get("owner") or OPERATOR
    tail = (f"; {len(failed)} of them did not finish ({', '.join(x['id'] for x in failed[:5])})"
            if failed else "")
    _message(team_dir, owner,
             f"Every task under {pid} “{parent.get('title')}” is closed "
             f"({len(siblings)}{tail}). Close it if its work is done "
             f"(complete_task), or add what is still missing under it.", pid)


def list_tasks(team_dir: Path | str, project: str | None = None,
               owner: str | None = None, status: str | None = None) -> list[dict]:
    rows = list(snapshot(team_dir).values())
    if project:
        rows = [r for r in rows if r.get("project") == project]
    if owner:
        rows = [r for r in rows if r.get("owner") == owner]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    rows.sort(key=lambda r: r.get("updated_at") or "")
    return rows


# --- reminders --------------------------------------------------------------

def reminders(team_dir: Path | str) -> list[dict]:
    """Unfinished tasks that carry a reminder, due or not."""
    return [r for r in snapshot(team_dir).values()
            if r.get("status") not in TERMINAL | PAUSED and r.get("check_after")]


def unreadable(task: dict) -> bool:
    ca = task.get("check_after")
    return bool(ca) and not (isinstance(ca, str) and _CANONICAL.match(ca))


def _due_in(state: dict[str, dict], now: str) -> list[dict]:
    # Canonical UTC timestamps sort lexically in time order, so this is a
    # string comparison. Anything not canonical fires now rather than never.
    return [r for r in state.values()
            if r.get("status") not in TERMINAL | PAUSED and r.get("check_after")
            and (unreadable(r) or r["check_after"] <= now)]


def due(team_dir: Path | str, now: str | None = None) -> list[dict]:
    """Unfinished tasks whose reminder has come due."""
    return _due_in(snapshot(team_dir), now or _now())


def _describe(until: dict) -> str:
    t = until.get("type")
    if t == "file_exists":
        return f"{until.get('path')} exists"
    if t == "file_contains":
        return f"{until.get('path')} contains {until.get('text')!r}"
    if t == "pid_exited":
        return f"process {until.get('pid')} exited"
    return str(until)


def reminder_text(owed: list[dict]) -> str:
    lines = []
    for t in owed:
        line = f"- {t['id']} [{t.get('status')}] ({t.get('project')}): {t.get('title')}"
        if t.get("note"):
            line += f" — {t['note']}"
        if unreadable(t):
            line += (f" (its reminder time {t['check_after']!r} could not be "
                     f"read, so it fired now; set a new one if you still "
                     f"need it)")
        lines.append(line)
    plural = "reminder has" if len(owed) == 1 else "reminders have"
    return (f"{len(owed)} {plural} come due on tasks you own:\n"
            + "\n".join(lines)
            + "\n\nCheck on them now. Set a new check_after to be reminded "
              "again, or complete_task when one is finished.")


# (path, mtime_ns, size) -> earliest reminder in that version of the file.
# Lets an idle poll skip the read entirely: the log is only re-read when it
# has changed or when a reminder it already knew about has come due.
_quiet: dict[str, tuple] = {}


def _takes_keywords(send) -> bool:
    import inspect
    try:
        params = inspect.signature(send).parameters.values()
    except (TypeError, ValueError):
        return False
    names = {p.name for p in params}
    return ({"kind", "task_id"} <= names
            or any(p.kind is p.VAR_KEYWORD for p in params))


def sweep(team_dir: Path | str, send, owners=None,
          now: str | None = None) -> dict[str, list[dict]]:
    """Deliver every due reminder once, through `send(to, content) -> str`.

    One message per task, sent with kind="reminder" and the task's id when
    `send` takes those keywords (a transport's send_kind does). One message
    per owner used to carry them all with no task id, so a turn woken by a
    reminder recorded no task as its trigger and nothing could say which
    task an agent was busy with. It costs no extra wakes: an agent's mail is
    delivered in one turn however many messages it holds.

    A result starting with "[error" or an exception leaves that task's
    reminder armed for the next sweep, so a reminder is marked delivered
    only after the message went out. `owners`,
    if given, limits delivery to those names -- a supervisor passes the agents
    it can actually wake. Returns {owner: [tasks delivered]}.

    Safe to call from more than one process: the decision and the marking
    happen under the log's lock, so each reminder is delivered once.
    """
    p = path(team_dir)
    now = now or _now()
    try:
        st = p.stat()
    except OSError:
        return {}
    key = str(p)
    cached = _quiet.get(key)
    if (cached and cached[:2] == (st.st_mtime_ns, st.st_size)
            and (cached[2] is None or now < cached[2])):
        return {}

    delivered: dict[str, list[dict]] = {}
    notices: list[tuple] = []           # posted once the log's lock is let go
    with filelock.locked(p):
        state = snapshot(team_dir)
        by_owner: dict[str, list[dict]] = {}
        for t in _due_in(state, now):
            o = t.get("owner")
            if not o or (owners is not None and o not in owners):
                continue
            if t.get("every"):
                by_owner.setdefault(o, []).append(t)
                continue
            until = t.get("until")
            if until and not unreadable(t):
                if satisfied(until):
                    t = dict(t, note=(t.get("note") or "") +
                             f" [condition met: {_describe(until)}]")
                elif t.get("deadline") and t["deadline"] <= now:
                    t = dict(t, note=(t.get("note") or "") +
                             f" [deadline {t['deadline']} passed; condition NOT met: "
                             f"{_describe(until)}]")
                else:
                    # Not yet: look again later, and wake nobody.
                    every = t.get("poll_every") or "5m"
                    nxt = time.strftime(_FMT, time.gmtime(
                        time.time() + duration_seconds(every)))
                    _append(team_dir, {"id": t["id"], "kind": _KIND_SWEPT, "ts": now,
                                       "check_after": nxt})
                    continue
            by_owner.setdefault(o, []).append(t)
        keyed = _takes_keywords(send)
        for owner, owed in by_owner.items():
            for t in owed:
                run = duty_miss = None
                if t.get("every"):
                    run = duties.catch_up(t["every"], t["check_after"], duties._epoch(now))
                    duty_miss = t.get("run_due")
                    text = duties.reminder_text(t, run, duty_miss)
                else:
                    text = reminder_text([t])
                try:
                    result = (send(owner, text, kind="reminder", task_id=t["id"]) if keyed
                              else send(owner, text))
                except Exception:
                    continue
                if isinstance(result, str) and result.lstrip().startswith("[error"):
                    continue
                if run is None:
                    _append(team_dir, {"id": t["id"], "kind": _KIND_SWEPT,
                                       "ts": now, "check_after": None})
                else:
                    missed = ([duty_miss] if duty_miss else []) + run["skipped"]
                    entry = {"id": t["id"], "kind": _KIND_SWEPT, "ts": now,
                             "check_after": run["next"], "run_due": run["run"], "run": "due"}
                    if missed:
                        entry.update(missed=(t.get("missed") or 0) + len(missed),
                                     missed_total=(t.get("missed_total") or 0) + len(missed),
                                     last_missed=missed[-1], run_missed=len(missed),
                                     run_missed_why=("not done" if duty_miss else "")
                                     + (" and " if duty_miss and run["skipped"] else "")
                                     + ("nothing delivered" if run["skipped"] else ""))
                        if t.get("channel"):
                            notices.append((t, len(missed), run))
                    _append(team_dir, entry)
                delivered.setdefault(owner, []).append(t)
        remaining = [r for r in snapshot(team_dir).values()
                     if r.get("status") not in TERMINAL | PAUSED and r.get("check_after")]
        if not remaining:
            earliest = None
        elif any(unreadable(r) for r in remaining):
            earliest = ""           # never skip: those are due every pass
        else:
            earliest = min(r["check_after"] for r in remaining)
        try:
            st = p.stat()
            _quiet[key] = (st.st_mtime_ns, st.st_size, earliest)
        except OSError:
            _quiet.pop(key, None)
    for t, n, run in notices:
        try:
            _notice(team_dir, get(team_dir, t["id"]) or t, "supervisor",
                    f"{n} run{'s' if n != 1 else ''} missed; the run due {run['run']} "
                    f"went to {t.get('owner')}")
        except Exception:
            pass
    return delivered


# --- command line -------------------------------------------------------------

def main(argv=None) -> int:
    """`python -m ateam.tasks sweep|list` -- for hosts the supervisor does not drive.

    `sweep` is the whole of what a supervisor does for reminders, runnable by
    any scheduler a host already has (a systemd timer, cron, a loop in the
    host's own daemon): one entry for the whole team, not one per agent.
    """
    ap = argparse.ArgumentParser(prog="ateam.tasks")
    ap.add_argument("command", choices=("sweep", "list", "waive"))
    ap.add_argument("task_id", nargs="?", help="for waive: the task")
    ap.add_argument("--by", default="user", help="for waive: who is waiving (default: user)")
    ap.add_argument("--reason", default="", help="for waive: why, for the record")
    ap.add_argument("--team-dir", default=None)
    a = ap.parse_args(argv)

    from . import scope
    td = scope.team_dir(a.team_dir)
    os.environ["ATEAM_TEAM_DIR"] = str(td)

    if a.command == "waive":
        if not a.task_id:
            ap.error("waive needs a task id")
        try:
            entry = waive_review(td, a.task_id, by=a.by, reason=a.reason)
        except TaskError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"review waived for {a.task_id} by {entry['reviewer']}: {entry['findings']}")
        return 0

    if a.command == "list":
        for t in list_tasks(td):
            when = f"  check after {t['check_after']}" if t.get("check_after") else ""
            print(f"{t['id']}  {t.get('status'):<9} {t.get('owner') or '-':<10} "
                  f"({t.get('project')}) {t.get('title')}{when}")
        return 0

    from .transport import load as load_transport
    bus = load_transport("supervisor")
    try:
        delivered = sweep(td, bus.send_kind)
    finally:
        if hasattr(bus, "close"):
            bus.close()
    for owner, owed in delivered.items():
        print(f"reminded {owner}: {', '.join(t['id'] for t in owed)}")
    if not delivered:
        print("no reminders due")
    return 0


if __name__ == "__main__":
    sys.exit(main())
