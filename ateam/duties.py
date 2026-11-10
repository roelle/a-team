"""Standing duties: work that never finishes, on a schedule of its own.

## Why this file exists

Asked for: "Standing duties have no home. Some work never finishes:
sweeping the user's email and chat, watching a dashboard where push is not
possible. If a turn fails, the task is closed; if the host is down when
the reminder fires, the duty stops silently. No record shows it was
supposed to run and no agent hears about the miss."

A reminder fires once and the agent re-arms it. A turn that failed, an
agent that closed the task when it finished a pass, or a supervisor that was
down when the reminder came due each ended the duty, and nothing recorded
that it should have run. So a duty is a task with `every`, and the schedule
is the sweep's to keep, not the agent's: each pass that finds a run due
delivers it and sets the next one from the schedule, whatever became of the
last. complete_task on a duty records that run and leaves the duty open.

## What counts as missed

A run is missed when it was due and never marked done: the next one came
due first. Runs that fell due while nothing swept (the host was down, the
team stopped) are missed too, counted, and caught up with one run, not one
per miss. Each miss is on the task's record, the owner is told in the
reminder that follows, and a duty that has missed twice running is on the
operator's Waiting on you.

## Schedules

`every` is a duration ("30m", "2h", "1d"), a run counted from the last
due time, or a time of day: "daily 08:00", "weekdays 17:30", "mon,thu
09:00", each with an IANA zone ("weekdays 08:00 America/Los_Angeles"). A
time of day given without a zone takes this host's and is stored with it,
so it does not move when another host reads it.
"""
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FMT = "%Y-%m-%dT%H:%M:%SZ"
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$", re.IGNORECASE)
_UNITS = {}
for _names, _secs in ((("m", "min", "mins", "minute", "minutes"), 60),
                      (("h", "hr", "hrs", "hour", "hours"), 3600),
                      (("d", "day", "days"), 86400),
                      (("w", "wk", "wks", "week", "weeks"), 604800)):
    _UNITS.update(dict.fromkeys(_names, _secs))
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_SETS = {"daily": _DAYS, "weekdays": _DAYS[:5], "weekends": _DAYS[5:]}
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")

#: Shorter than this is a loop, not a duty; a condition to wait on is `until`.
MIN_SECONDS = 300

HELP = ("a duration like '30m', '2h' or '1d', or a time of day like "
        "'daily 08:00', 'weekdays 17:30' or 'mon,thu 09:00', with a zone if "
        "it is not this host's ('weekdays 08:00 America/Los_Angeles')")


class DutyError(ValueError):
    """A schedule that cannot be kept, phrased as a sentence."""


def local_zone() -> str | None:
    """This host's IANA zone, or None if it cannot be told."""
    tz = (os.environ.get("TZ") or "").lstrip(":")
    if tz and _zone(tz):
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    _, sep, name = target.partition("zoneinfo/")
    return name if sep and _zone(name) else None


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def parse(raw) -> str:
    """`every`, normalised: '30m' as written, or 'weekdays 08:00 <Zone>'."""
    if not isinstance(raw, str) or not raw.strip():
        raise DutyError(f"every is empty; give {HELP}")
    s = " ".join(raw.strip().split())
    m = _DURATION.match(s)
    if m and m.group(2).lower() in _UNITS:
        if float(m.group(1)) * _UNITS[m.group(2).lower()] < MIN_SECONDS:
            raise DutyError(f"every {raw!r} is more often than every 5 minutes: "
                            f"that is a loop, not a duty. To wait for something "
                            f"to happen, give the task `until` instead")
        return s
    if s.lower() in ("hourly",):
        return "1h"
    parts = s.split(" ")
    if len(parts) not in (2, 3):
        raise DutyError(f"every {raw!r} is not a schedule this can keep; give {HELP}")
    days, clock = parts[0].lower(), parts[1]
    zone = parts[2] if len(parts) == 3 else local_zone()
    if days not in _DAY_SETS and not all(d in _DAYS for d in days.split(",")):
        raise DutyError(f"every {raw!r}: {parts[0]!r} is not days this knows; give "
                        f"daily, weekdays, weekends or days like 'mon,thu'")
    c = _CLOCK.match(clock)
    if not c or int(c.group(1)) > 23 or int(c.group(2)) > 59:
        raise DutyError(f"every {raw!r}: {clock!r} is not a time of day; give it "
                        f"as HH:MM, 24-hour")
    if not zone:
        raise DutyError(f"every {raw!r} has no zone and this host's could not be "
                        f"told, so it would run at a different hour on every "
                        f"host; add one, like 'America/Los_Angeles' or 'UTC'")
    if not _zone(zone):
        raise DutyError(f"every {raw!r}: {zone!r} is not a time zone; give an "
                        f"IANA name like 'America/Los_Angeles' or 'UTC'")
    return f"{days} {int(c.group(1)):02d}:{c.group(2)} {zone}"


def period_seconds(every: str) -> float:
    """How far apart runs are, at most: for spacing and for 'late'."""
    m = _DURATION.match(every)
    if m and m.group(2).lower() in _UNITS:
        return float(m.group(1)) * _UNITS[m.group(2).lower()]
    days = every.split(" ")[0]
    picked = _DAY_SETS.get(days) or days.split(",")
    gaps = sorted(_DAYS.index(d) for d in picked)
    widest = max([(b - a) for a, b in zip(gaps, gaps[1:])] + [7 - gaps[-1] + gaps[0]])
    return widest * 86400.0


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, _FMT).replace(tzinfo=timezone.utc).timestamp()


def _stamp(epoch: float) -> str:
    return time.strftime(_FMT, time.gmtime(epoch))


def next_after(every: str, after: float, anchor: float | None = None) -> float:
    """The first run strictly after `after` (epoch seconds). A duration
    counts from `anchor`, the run before (else from `after`)."""
    m = _DURATION.match(every)
    if m and m.group(2).lower() in _UNITS:
        step = float(m.group(1)) * _UNITS[m.group(2).lower()]
        if anchor is None:
            return after + step
        n = max(1, int((after - anchor) // step) + 1)
        return anchor + n * step
    days, clock, zone = every.split(" ")
    picked = set(_DAY_SETS.get(days) or days.split(","))
    tz = _zone(zone)
    hh, mm = (int(x) for x in clock.split(":"))
    local = datetime.fromtimestamp(after, tz)
    for i in range(0, 9):
        day = (local + timedelta(days=i)).date()
        if _DAYS[day.weekday()] not in picked:
            continue
        at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).timestamp()
        if at > after:
            return at
    raise DutyError(f"no run of {every!r} in the next week")      # cannot happen


def first_run(every: str, now: float | None = None) -> str:
    """When a new duty first runs: now, for a duration; else its first time."""
    now = time.time() if now is None else now
    m = _DURATION.match(every)
    if m and m.group(2).lower() in _UNITS:
        return _stamp(now)
    return _stamp(next_after(every, now - 1))


def catch_up(every: str, due: str, now: float | None = None) -> dict:
    """A run that came due at `due` is being delivered at `now`.

    Returns {"run": the due time this delivery is for (the latest one that
    has come), "skipped": [due times before it that nothing delivered],
    "next": when the one after is due}. Times are canonical UTC.
    """
    now = time.time() if now is None else now
    try:
        t = _epoch(due)
    except ValueError:
        t = now
    skipped, anchor = [], t
    while True:
        nxt = next_after(every, anchor, anchor)
        if nxt > now:
            break
        skipped.append(_stamp(anchor))
        anchor = nxt
        if len(skipped) > 10000:
            break
    return {"run": _stamp(anchor), "skipped": skipped, "next": _stamp(next_after(every, now, anchor))}


def reminder_text(t: dict, run: dict, missed_last: str | None) -> str:
    """The message that delivers one run of a duty."""
    lines = [f"Standing duty {t['id']} “{t.get('title')}” (every {t['every']}): "
             f"the run due {run['run']} is yours now."]
    if t.get("note"):
        lines.append(f"What it is: {t['note']}")
    if missed_last:
        lines.append(f"Your last run, due {missed_last}, was never marked done, so it "
                     f"is recorded as missed.")
    if run["skipped"]:
        n = len(run["skipped"])
        lines.append(f"{n} run{'s' if n != 1 else ''} between {run['skipped'][0]} and "
                     f"{run['skipped'][-1]} fell due while nothing could deliver them "
                     f"(the team was stopped or the host down); they are recorded "
                     f"as missed. This run catches up: cover what they would have.")
    lines.append(f"Do it, then complete_task({t['id']!r}, note=what you found) to record "
                 f"this run. The duty stays open; the next run is due {run['next']}. "
                 f"To end the duty, ask the operator.")
    return "\n".join(lines)


def late(t: dict, now: float | None = None) -> str | None:
    """Why a duty is behind, for someone looking at it now, or None.

    Read from the task alone, so it shows when nothing is sweeping: a run
    due longer ago than a period (and at least fifteen minutes) that no
    sweep delivered."""
    if not t.get("every") or not t.get("check_after"):
        return None
    now = time.time() if now is None else now
    try:
        due = _epoch(t["check_after"])
    except ValueError:
        return None
    grace = max(900.0, period_seconds(t["every"]))
    if now - due > grace:
        return f"its run due {t['check_after']} has not been delivered"
    return None
