"""Where a memory came from, so its text is not all anyone has to go on.

An agent's memory carries a date and the agent's own reason. Neither says what
it had just read, and a memory is offered back as something the agent learned:
a web page that talks an agent into a bad lesson would keep working long after
the page was gone. So every save is stamped with the turn it happened in, who
that turn's mail came from, and any outside content in the conversation so far.

Outside means a page, a download, or a tool this file does not know: an unknown
tool counts, because being wrong in that direction costs one extra look. What
is read is kept per conversation, since an agent still has it in context on
later turns, which is exactly where a distill turn gets its lessons from.

This is a record, not a verdict. A memory with no sources is not thereby safe:
a file in the repository can say anything, and a shell command can reach the
network in ways the pattern below does not see. Bookkeeping must never break a
turn or a save, so a problem here means no stamp.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import filelock

DIR = "origin"
#: Newest sources kept per conversation, and shown per memory.
MAX_SOURCES = 12
#: Mail ids stamped on a save, enough to find the turn's mail again.
MAX_MAIL = 5

#: Tools that work on this machine and the team's own record. Anything else,
#: including a tool added by some MCP server, counts as outside.
LOCAL = frozenset("""
    Read Write Edit NotebookEdit Glob Grep Agent TodoWrite
    view_file edit_file create_file find_file search_directory list_directory
    invoke_subagent
    broadcast check_inbox list_channels list_retros list_reviews list_teammates
    post_to_channel record_retro record_review send_to_teammate team_status
    start_team stop_team roster_add roster_remove grant_workspace revoke_workspace
    claim_task complete_task create_task list_tasks update_task
    my_activity my_capabilities whoami
    save_memory read_memory delete_memory merge_memories memory_index review_memory
    """.split())
SHELLS = frozenset(("Bash", "run_command"))
#: A command word, not the same word as an argument to grep.
_START = r"(?:^|[;&|(`]\s*|\bsudo\s+|\btime\s+|\bxargs\s+)"
_NET = re.compile(_START + r"(curl|wget|gh|ssh|scp|rsync|sftp|ftp|nc|http|https|lynx|aria2c)\s")
_GIT = re.compile(_START + r"git\s+(?:-\S+\s+)*(clone|fetch|pull)\b")
_URL = re.compile(r"https?://[^\s'\"<>)]+")


def _path(team_dir, agent: str) -> Path:
    return Path(team_dir) / DIR / f"{agent}.json"


def _host(text) -> str:
    """The host of the first URL in `text`. Only the host: a query string or a
    path can carry a token, and this ends up on a page and in a prompt."""
    found = _URL.search(str(text or ""))
    if not found:
        return ""
    try:
        return urlsplit(found.group(0)).hostname or ""
    except ValueError:
        return ""


def _named(tool: str, what: str) -> str:
    return f"{tool} {what}" if what else tool


def outside(tool: str, args) -> str:
    """What this call read from outside, as a short label; "" for a local call."""
    args = args if isinstance(args, dict) else {}
    if tool in SHELLS:
        cmd = str(args.get("command") or args.get("cmd") or args.get("CommandLine") or "")
        hit = _NET.search(cmd) or _GIT.search(cmd)
        return _named(hit.group(1) if hit else "", _host(cmd)) if hit else ""
    if tool in LOCAL:
        return ""
    return _named(tool, _host(json.dumps(args, default=str)))


def note(team_dir, agent: str, tool: str, args) -> None:
    """Remember a call that read outside content, for this conversation."""
    try:
        label = outside(tool, args)
        if not label:
            return
        path = _path(team_dir, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.locked(path):
            seen = _read(path)
            if label in seen:
                seen.remove(label)
            seen.append(label)
            path.write_text(json.dumps(seen[-MAX_SOURCES:]), encoding="utf-8")
    except Exception:
        pass


def _read(path: Path) -> list[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(x) for x in doc] if isinstance(doc, list) else []


def sources(team_dir, agent: str) -> list[str]:
    """Outside content in the agent's current conversation."""
    try:
        return _read(_path(team_dir, agent))
    except Exception:
        return []


def forget(team_dir, agent: str | None = None) -> None:
    """A new conversation has none of it in context. Failing to forget only
    marks more memories than it should, which is the safe way to be wrong."""
    try:
        if agent is None:
            for p in (Path(team_dir) / DIR).glob("*.json"):
                p.unlink(missing_ok=True)
        else:
            _path(team_dir, agent).unlink(missing_ok=True)
    except Exception:
        pass


def stamp(team_dir, agent: str) -> dict:
    """What to log with a save: {} outside a supervised turn, which is a
    person's own session and theirs to vouch for."""
    if team_dir is None:
        return {}
    try:
        from . import steer
        turn = steer.turn_of(team_dir, agent)
        if not turn:
            return {}
        msgs = [m for m in turn.get("msgs") or [] if isinstance(m, dict)]
        out = {"turn": turn.get("ts"),
               "from": sorted({str(m.get("from")) for m in msgs if m.get("from")})}
        ids = [m["id"] for m in msgs if m.get("id") is not None][:MAX_MAIL]
        if ids:
            out["mail"] = ids
        seen = sources(team_dir, agent)
        if seen:
            out["outside"] = seen
        return out
    except Exception:
        return {}
