"""Operator actions: what the human running a team can do to it, in one place.

Every client -- the command line, a chat app, a script -- changes the team
through these functions, so the same checks apply whichever one is used:

    post(team_dir, "billing", "@coder can you look at allocate?", author="matt")
    dm(team_dir, "coder", "quick question", author="matt")
    create_channel(team_dir, "sims", ["coder", "syseng"], topic="long runs")
    archive_channel(team_dir, "sims") / restore_channel / delete_channel
    create_task(team_dir, "billing", "fix allocate", owner="coder", channel="billing")
    update_task(team_dir, "task_ab12cd34", status="icebox")
    set_model(team_dir, "qa", "gemini-3.1-pro-preview", until="3d")
    stop(team_dir, "maintenance") / start(team_dir) / restart(team_dir)
    add_agent / update_agent / remove_agent      the roster
    norms / set_norms                            NORMS.md
    memories / save_memory / delete_memory       one agent's memories
    archived_memories / restore_memory / review_memory

Messages from here are sent as "user" -- the operator's identity on the bus,
which the review gate and the episode logic depend on -- and carry the
person's name as `author`, so a team with more than one human can tell them
apart without a migration.

Each refuses to run inside an agent's session (ATEAM_AGENT set): these are
the operator's, and an agent that could call them could speak as the user.
"""
import json
import os
import re
import time
from pathlib import Path

from . import channels as channels_lib


class OperatorError(ValueError):
    pass


def _guard() -> None:
    inside = os.environ.get("ATEAM_AGENT", "").strip()
    if inside:
        raise OperatorError(f"operator actions are the operator's, and this is "
                            f"{inside}'s session")


def _user_transport(team_dir):
    from .transport_file import FileTransport
    from .transport import load
    if os.environ.get("ATEAM_BUS_TRANSPORT"):
        return load("user")
    return FileTransport("user", {"team_dir": str(team_dir)})


def post(team_dir, channel: str, content: str, author: str | None = None,
         thread: str | None = None, kind: str = "work") -> dict:
    """Post to a channel as the user. Wakes only the agents it mentions."""
    _guard()
    if not (content or "").strip():
        raise OperatorError("nothing to post")
    t = _user_transport(team_dir)
    try:
        return channels_lib.post(t, team_dir, channel, content, kind=kind,
                                 thread=thread, author=author)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


# --- editing and deleting what you sent ---------------------------------------
#
# A chat client lets you fix a message after sending it. Here a message is a
# prompt: an agent may already have acted on it, and quietly changing the
# text would leave the record saying one thing and the agent having done
# another. So an edit or a delete does what is true for each recipient. A
# copy still unread in its inbox is changed in place (or taken out): it will
# only ever be read as it now stands. A recipient that has read it, or is
# reading it in a turn now, is sent a short note with the new text (or
# telling it to disregard the old), in the same thread, waking it if the
# original did. The record keeps both: bus.jsonl as sent, edits.jsonl with
# each change and who was told, and Record.bus() shows the message as it now
# stands, marked edited or deleted.

EDITS = "edits.jsonl"


def _sent(team_dir, msg_id: str) -> dict:
    from .record import _jsonl
    row = next((r for r in _jsonl(Path(team_dir) / "bus.jsonl") if r.get("id") == msg_id), None)
    if row is None:
        raise OperatorError(f"no message {msg_id!r}")
    if row.get("from") != "user":
        raise OperatorError("only what you sent can be edited or deleted; an agent's "
                            "message is the team's record")
    from .record import Record
    now = next((r for r in Record(team_dir).bus() if r.get("id") == msg_id), row)
    if now.get("deleted"):
        raise OperatorError("that message was deleted")
    return now


def _could_have_read(team_dir, row: dict) -> dict[str, bool]:
    """{agent: whether the message woke it} for every agent it went to."""
    doc = channels_lib._roster_doc(team_dir)
    names = [a["name"] for a in doc.get("agents", []) if isinstance(a, dict) and a.get("name")]
    out: dict[str, bool] = {}
    if row.get("channel"):
        chan = channels_lib.load(team_dir).get(row["channel"]) or {}
        named = set(row.get("mentions") or [])
        listening = set(chan.get("wake") or ())
        for n in chan.get("members") or []:
            out[n] = n in named or n in listening
        for n in named:
            if n in names:
                out[n] = True
    elif row.get("to") and row["to"] != "user":
        out[row["to"]] = True
    from . import roster as roster_lib
    for handle in channels_lib._MENTION.findall(row.get("content") or ""):
        who = roster_lib.resolve_name(handle, names, roster_lib.aliases(doc))
        if who and who != "user":
            out[who] = True
    return {n: w for n, w in out.items() if n in names}


def _being_read(team_dir) -> dict[str, set]:
    """{agent: ids of the messages in the turn it is in now}."""
    try:
        doc = json.loads((Path(team_dir) / "inflight.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {a: {m.get("id") for m in (t.get("msgs") or []) if isinstance(m, dict)}
            for a, t in (doc.items() if isinstance(doc, dict) else []) if isinstance(t, dict)}


def _revise(team_dir, msg_id: str, content: str | None, by: str | None,
            own_only: bool = False) -> dict:
    _guard()
    if os.environ.get("ATEAM_BUS_TRANSPORT"):
        raise OperatorError("editing a sent message needs the file transport, "
                            "whose inboxes it can change")
    from . import filelock
    from .transport_file import FileTransport
    row = _sent(team_dir, msg_id)
    if by and (row.get("author") or own_only) and row.get("author") != by:
        raise OperatorError(f"{row.get('author') or 'the operator'} sent that message; "
                            f"only they can change it")
    if content is not None and content.strip() == (row.get("content") or "").strip():
        raise OperatorError("nothing changed")
    t = FileTransport("user", {"team_dir": str(team_dir)})
    reading = _being_read(team_dir)
    changed, told = [], []
    when = row.get("ts", "")
    for agent, woke in sorted(_could_have_read(team_dir, row).items()):
        inbox = t._inbox(agent)
        unread = False
        with filelock.locked(inbox):
            try:
                lines = inbox.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            kept = []
            for line in lines:
                try:
                    e = json.loads(line)
                except ValueError:
                    kept.append(line)
                    continue
                if e.get("id") == msg_id and msg_id not in reading.get(agent, set()):
                    unread = True
                    if content is not None:
                        kept.append(json.dumps({**e, "content": content}))
                    continue
                kept.append(line)
            if unread:
                t._rewrite(inbox, "".join(l + "\n" for l in kept))
                changed.append(agent)
                continue
            quote = (row.get("content") or "").strip().replace("\n", " ")
            quote = quote[:80] + ("..." if len(quote) > 80 else "")
            note = (f"[edited] I changed a message I sent you at {when} after you "
                    f"had it. Act on this version instead:\n\n{content}"
                    if content is not None else
                    f"[withdrawn] Disregard my message sent at {when} "
                    f"(\u201c{quote}\u201d); I withdrew it. If you already acted "
                    f"on it, say what you did.")
            meta = {"thread": row.get("thread") or msg_id, "channel": row.get("channel"),
                    "author": row.get("author"), "quiet": None if woke else True}
            entry = t._entry("user", agent, note, row.get("kind") or "work", row.get("task_id"),
                             **{k: v for k, v in meta.items() if v})
            entry["note_of"] = msg_id
            with inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            told.append(agent)
    rec = {"id": msg_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by or "user",
           "changed_unread": changed, "told": told}
    if content is None:
        rec["deleted"] = True
    else:
        rec["content"] = content
    path = Path(team_dir) / EDITS
    with filelock.locked(path):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return {"id": msg_id, "changed_unread": changed, "told": told,
            "deleted": content is None}


def edit_message(team_dir, msg_id: str, content: str, by: str | None = None,
                 own_only: bool = False) -> dict:
    """Change a message you sent. Returns {"changed_unread": agents whose
    unread copy now reads the new text, "told": agents sent the change}."""
    if not (content or "").strip():
        raise OperatorError("an edit needs text; delete the message instead")
    return _revise(team_dir, msg_id, content.strip(), by, own_only)


def delete_message(team_dir, msg_id: str, by: str | None = None,
                   own_only: bool = False) -> dict:
    """Withdraw a message you sent: taken out of every inbox that has not
    read it, and a note to disregard it to every agent that has."""
    return _revise(team_dir, msg_id, None, by, own_only)


def clear_waiting(team_dir, ids=(), sender: str | None = None, by: str | None = None) -> int:
    """Take messages off Waiting on you without replying: `ids` (each row's
    "key"), and all from `sender`. For a notice, which nothing reads a reply to, or a question that
    no longer matters. The message stays where it was sent and its sender is
    not told. Returns how many came off."""
    _guard()
    from . import filelock
    from .record import CLEARED, Record
    gone = [r["key"] for r in Record(team_dir).needs_you()
            if r.get("key") and (r["key"] in ids or (sender and r.get("from") == sender))]
    path = Path(team_dir) / CLEARED
    with filelock.locked(path), path.open("a") as f:
        for i in gone:
            f.write(json.dumps({"id": i, "by": by, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
    return len(gone)


def dm(team_dir, agent: str, content: str, author: str | None = None,
       thread: str | None = None, kind: str = "work") -> str:
    """A direct message from the user. It wakes the agent: it was addressed."""
    _guard()
    if not (content or "").strip():
        raise OperatorError("nothing to send")
    t = _user_transport(team_dir)
    meta = {"author": author, "thread": thread}
    try:
        out = t.send(agent, content, kind=kind, **{k: v for k, v in meta.items() if v})
    except TypeError:
        out = t.send_kind(agent, content, kind=kind)
    if out.startswith("[error"):
        raise OperatorError(out.strip("[]"))
    # An @mention in a direct message brings that agent in, as it would in a
    # channel: it gets a copy, which wakes it, saying where it was said.
    # Without this, "@ada can you help?" in a DM (or a DM's thread) reached
    # only the agent the DM was to. Group words (@all, @here) do nothing here.
    from . import roster as roster_lib
    doc = channels_lib._roster_doc(team_dir)
    names = [a["name"] for a in doc.get("agents", []) if isinstance(a, dict) and a.get("name")]
    pulled = []
    for handle in channels_lib._MENTION.findall(content):
        who = roster_lib.resolve_name(handle, names, roster_lib.aliases(doc))
        if who and who not in (agent, "user") and who not in pulled:
            pulled.append(who)
    for who in pulled:
        note = (f"[You were mentioned in the operator's direct message to {agent}. "
                f"Reply to the operator, or to {agent} if it is theirs to answer.]\n")
        try:
            t.send(who, note + content, kind=kind, **{k: v for k, v in meta.items() if v})
        except TypeError:
            t.send_kind(who, note + content, kind=kind)
    if pulled:
        out += f" (and to {', '.join(pulled)}, who were mentioned)"
    return out


def create_channel(team_dir, name: str, members=(), topic: str = "",
                   everyone: bool = False) -> dict:
    _guard()
    try:
        return channels_lib.create(team_dir, name, list(members), topic, everyone=everyone)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


def set_members(team_dir, name: str, add=(), remove=(),
                everyone: bool | None = None) -> list[str]:
    _guard()
    try:
        return channels_lib.set_members(team_dir, name, add, remove, everyone=everyone)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


def set_channel_wake(team_dir, name: str, agents=()) -> list[str]:
    """Which members of a channel every post to it wakes, mention or not."""
    _guard()
    try:
        return channels_lib.set_wake(team_dir, name, agents)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


def set_topic(team_dir, name: str, topic: str) -> None:
    _guard()
    try:
        channels_lib.set_topic(team_dir, name, topic)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


def archive_channel(team_dir, name: str) -> dict:
    """Read-only and out of every agent's list; restore_channel undoes it."""
    _guard()
    try:
        return channels_lib.archive(team_dir, name)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


def delete_channel(team_dir, name: str) -> dict:
    """Archive it and hide its history from clients. Its messages stay in
    bus.jsonl, the team's record; restore_channel brings it all back."""
    _guard()
    try:
        return channels_lib.delete(team_dir, name)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


def restore_channel(team_dir, name: str) -> dict:
    _guard()
    try:
        return channels_lib.restore(team_dir, name)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


# --- tasks: what moving a card does -----------------------------------------
#
# The operator may change any task. Whoever owns it is told: quietly, except
# when the change means they should stop (iced or cancelled while they were
# on it) or start (it was handed to them), which wakes them.

_BUSY = {"claimed", "running", "blocked", "review"}


def create_task(team_dir, project: str, title: str, owner: str | None = None,
                note: str = "", channel: str | None = None, status: str | None = None,
                deadline: str | None = None, requires_review: bool = False,
                rank: float | None = None, parent: str | None = None,
                level: int | None = None, priority: int | None = None,
                auto_close: bool = False, blocked_by: list[str] | None = None,
                writes: list[str] | None = None, thread: str | None = None,
                origin: dict | None = None, every: str | None = None) -> dict:
    _guard()
    from . import tasks
    try:
        t = tasks.create(team_dir, project, title, owner=owner or None, created_by="user",
                         note=note, channel=channel, status=status, deadline=deadline,
                         requires_review=requires_review, rank=rank, parent=parent or None,
                         level=level if level not in ("", None) else None,
                         priority=priority if priority not in ("", None) else None,
                         auto_close=bool(auto_close), blocked_by=blocked_by, writes=writes,
                         thread=thread, origin=origin, every=every or None)
    except tasks.TaskError as e:
        raise OperatorError(str(e)) from None
    if t.get("owner") and t.get("status") not in tasks.PAUSED:
        what = (f"a standing duty from the operator, every {t['every']}: {t['id']} "
                f"\u201c{t['title']}\u201d. Each run comes to you as a reminder; "
                f"complete_task records it and the duty stays open"
                if t.get("every") else
                f"a new task from the operator: {t['id']} \u201c{t['title']}\u201d")
        _tell(team_dir, t["owner"], t, f"You have {what}.", wake=not t.get("every"))
    return t


def update_task(team_dir, task_id: str, **changes) -> dict:
    """status, owner, title, project, note, channel ("" unlinks), deadline,
    rank, parent ("" for top level), level ("auto" to work it out), priority,
    auto_close, archived, blocked_by, writes, every ("" makes a duty an
    ordinary task)."""
    _guard()
    from . import tasks
    allowed = {"status", "owner", "title", "project", "note", "channel", "deadline", "rank",
               "parent", "level", "priority", "auto_close", "archived",
               "blocked_by", "writes", "every"}
    unknown = set(changes) - allowed
    if unknown:
        raise OperatorError(f"cannot change {', '.join(sorted(unknown))} from here")
    before = tasks.get(team_dir, task_id)
    if before is None:
        raise OperatorError(f"no task {task_id}")
    try:
        t = tasks.update(team_dir, task_id, "user", **{k: v for k, v in changes.items()
                                                      if v is not None})
    except tasks.TaskError as e:
        raise OperatorError(str(e)) from None
    old_owner, owner = before.get("owner"), t.get("owner")
    if owner and owner != old_owner:
        _tell(team_dir, owner, t, f"The operator handed you task {t['id']} "
                                  f"\u201c{t['title']}\u201d ({t['status']}).",
              wake=t["status"] not in tasks.PAUSED | tasks.TERMINAL)
        if old_owner:
            _tell(team_dir, old_owner, t, f"The operator handed your task {t['id']} "
                                          f"\u201c{t['title']}\u201d to {owner}.", wake=False)
    elif owner and t.get("status") != before.get("status"):
        stop = (t["status"] in tasks.PAUSED | {"cancelled"} and before.get("status") in _BUSY)
        _tell(team_dir, owner, t, f"The operator moved your task {t['id']} "
                                  f"\u201c{t['title']}\u201d from {before.get('status')} to "
                                  f"{t['status']}." + (" Stop work on it." if stop else ""),
              wake=stop)
    return t


def _tell(team_dir, agent: str, t: dict, text: str, wake: bool) -> None:
    if agent == "user":         # the operator's own task: a follow-up, say
        return
    try:
        tr = _user_transport(team_dir)
        tr.send_kind(agent, text, kind="work" if wake else "fyi", task_id=t["id"])
    except Exception:
        pass


def set_model(team_dir, agent: str, model: str, until: str | None = None,
              reason: str = "", by: str | None = None) -> dict:
    from . import models
    try:
        return models.set_model(team_dir, agent, model, until=until, reason=reason, by=by)
    except models.ModelError as e:
        raise OperatorError(str(e)) from None


def clear_model(team_dir, agent: str, by: str | None = None) -> dict:
    from . import models
    try:
        return models.clear(team_dir, agent, by=by)
    except models.ModelError as e:
        raise OperatorError(str(e)) from None


def stop(team_dir, reason: str = "") -> dict:
    _guard()
    from . import lifecycle
    return lifecycle.stop_team(reason=reason, team_dir=team_dir)


def start(team_dir) -> dict:
    _guard()
    from . import lifecycle
    return lifecycle.start_team(team_dir=team_dir)


def waive_review(team_dir, task_id: str, by: str, reason: str = "") -> dict:
    from . import tasks
    try:
        return tasks.waive_review(team_dir, task_id, by=by, reason=reason)
    except tasks.TaskError as e:
        raise OperatorError(str(e)) from None


def restart(team_dir, now: bool = False, by: str = "user") -> dict:
    """Ask the team's supervisor to drain and restart on the installed code:
    no new turns, the ones in flight finish (or, with `now`, are cancelled
    and their mail goes back), then it restarts itself. Refused when no
    supervisor runs, or when the one running predates graceful restart."""
    _guard()
    from . import heartbeat
    from .supervisor import request_restart
    beat = heartbeat.running(team_dir)
    if not beat:
        raise OperatorError("no supervisor is running this team; start it instead")
    if not beat.get("restartable"):
        raise OperatorError("the supervisor running this team predates graceful restart; "
                            "stop it and start it again (or run python -m ateam.update)")
    return request_restart(team_dir, now=now, by=by)



# --- a new team ---------------------------------------------------------------

def create_team(teams_root, name: str, runtime: str) -> dict:
    """A team on the starter roster, on one of starter.runtimes() that is
    ready. The agy runtime reaches its tools through the plugin, so that is
    installed first."""
    _guard()
    from . import install_plugin, starter
    ready = {r["id"]: r for r in starter.runtimes() if r["ready"]}
    if runtime not in ready:
        raise OperatorError(f"runtime {runtime!r} is not ready on this machine")
    if runtime == "agy":
        done = install_plugin.install()
        if done.returncode:
            raise OperatorError(f"could not install the agy plugin: "
                                f"{(done.stderr or done.stdout).strip()[-400:]}")
    try:
        team_dir = starter.create_team(teams_root, name, ready[runtime]["runner"])
    except starter.StarterError as e:
        raise OperatorError(str(e)) from None
    return {"team_dir": str(team_dir), "note": ready[runtime]["note"]}


# --- the roster --------------------------------------------------------------
#
# Written through roster.save, which records each write, so a running
# supervisor adopts these as the operator's rather than putting them back.

_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_RESERVED = {"user", "supervisor", "here", "all", "everyone", "channel"}
# What an operator may set on an agent from a client. Policy fields
# (workspaces, refuse_paths, allowed_send_to, ...) stay in the roster file,
# where they are reviewed as what they are.
AGENT_FIELDS = ("role", "model", "effort", "memory_model", "memory_effort",
                "is_principal", "tools_off", "alias")


def _roster_path(team_dir) -> Path:
    return Path(team_dir) / "roster.json"


def _agent_fields(fields: dict) -> dict:
    bad = sorted(set(fields) - set(AGENT_FIELDS))
    if bad:
        raise OperatorError(f"cannot set {', '.join(bad)} here; settable: "
                            f"{', '.join(AGENT_FIELDS)}")
    out = {}
    for k, v in fields.items():
        if k in ("role", "model", "effort", "memory_model", "memory_effort", "alias"):
            out[k] = str(v or "").strip()
        elif k == "is_principal":
            out[k] = bool(v)
        elif k == "tools_off":
            items = v.split(",") if isinstance(v, str) else list(v or [])
            out[k] = [str(x).strip() for x in items if str(x).strip()]
    return out


def add_agent(team_dir, name: str, role: str = "", channels="all", **fields) -> dict:
    """Put an agent on the roster. `channels`: "all" (every open channel),
    a list of channel names, or None. Channels that follow the roster
    include it either way."""
    _guard()
    from . import lifecycle
    name = (name or "").strip()
    if not _AGENT_NAME.match(name) or name.lower() in _RESERVED:
        raise OperatorError(f"{name!r} cannot be an agent's name: letters, digits, "
                            f"'.', '_' and '-', not one of {', '.join(sorted(_RESERVED))}")
    extra = {k: v for k, v in _agent_fields(fields).items() if v not in ("", [], None)}
    try:
        out = lifecycle.add_agent(name, role=(role or "").strip(), team_dir=team_dir, **extra)
    except ValueError as e:
        raise OperatorError(str(e)) from None
    names = (list(channels_lib.active(team_dir)) if channels == "all"
             else list(channels or []))
    for chan in names:
        try:
            channels_lib.set_members(team_dir, chan, add=[name])
        except channels_lib.ChannelError:
            pass
    out["channels"] = channels_lib.of(team_dir, name)
    return out


def update_agent(team_dir, name: str, **fields) -> dict:
    """Change an agent's role, default model, effort, memory model and
    effort, principal flag, tools_off or alias (the name it is shown by and
    answers to as @alias). An empty value removes
    the field."""
    _guard()
    from . import roster as roster_lib
    changes = _agent_fields(fields)
    path = _roster_path(team_dir)
    doc = roster_lib.load(path)
    entry = next((a for a in doc.get("agents", []) if a.get("name") == name), None)
    if entry is None:
        raise OperatorError(f"no teammate named {name!r}")
    for k, v in changes.items():
        if v in ("", [], None) or (k == "is_principal" and v is False):
            entry.pop(k, None)
        else:
            entry[k] = v
    try:
        roster_lib.normalize(doc, source=str(path))
        roster_lib.aliases(doc)
    except roster_lib.RosterError as e:
        raise OperatorError(str(e)) from None
    roster_lib.save(path, doc)
    return entry


def remove_agent(team_dir, name: str) -> dict:
    """Take an agent off the roster and out of every channel. Its memories
    and history stay on disk; a running supervisor cancels its turn."""
    _guard()
    from . import lifecycle
    try:
        out = lifecycle.remove_agent(name, team_dir=team_dir)
    except ValueError as e:
        raise OperatorError(str(e)) from None
    for chan, c in channels_lib.load(team_dir).items():
        if name in c.get("members", []) and not c.get("everyone"):
            try:
                channels_lib.set_members(team_dir, chan, remove=[name])
            except channels_lib.ChannelError:
                pass
    return out


# --- norms ------------------------------------------------------------------

def norms(team_dir) -> str:
    try:
        return (Path(team_dir) / "NORMS.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def set_norms(team_dir, text: str, by: str = "user") -> dict:
    """Replace NORMS.md. Agents read it when a conversation starts, so a
    change reaches each agent on its next fresh conversation (cycle to make
    that now)."""
    _guard()
    import tempfile
    import time
    path = Path(team_dir) / "NORMS.md"
    text = (text or "").replace("\r\n", "\n")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".NORMS.md.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") or not text else text + "\n")
    os.replace(tmp, path)
    try:
        with (Path(team_dir) / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "event": "norms_edited", "by": by,
                                "bytes": len(text.encode("utf-8"))}) + "\n")
    except OSError:
        pass
    return {"path": str(path), "bytes": len(text.encode("utf-8"))}


# --- memories -----------------------------------------------------------------

def _memory_store(team_dir, agent: str):
    from . import memory_log
    try:
        return memory_log.open_store(team_dir, agent)
    except LookupError as e:
        raise OperatorError(str(e)) from None


def memories(team_dir, agent: str) -> list[dict]:
    """[{name, description, content}] for one agent."""
    store = _memory_store(team_dir, agent)
    try:
        return [{"name": e.name, "description": e.description, "content": store.read(e.name)}
                for e in store.index()]
    finally:
        store.close()


def save_memory(team_dir, agent: str, name: str, content: str, description: str = "",
                by: str = "user") -> dict:
    """Write one of an agent's memories, as the operator. The agent reads it
    as its own; `why` records who wrote it."""
    _guard()
    from . import memory as memory_lib
    try:
        key = memory_lib.normalize_name(name)
    except Exception as e:
        raise OperatorError(str(e)) from None
    if not (content or "").strip():
        raise OperatorError("a memory needs content")
    from . import memory_log
    store = _memory_store(team_dir, agent)
    try:
        new = store.save(key, (description or "").strip() or key, content,
                         why=f"written by the operator ({by})")
    finally:
        store.close()
    memory_log.record(team_dir, agent, "save", key, new=bool(new), by=by)
    return {"agent": agent, "name": key, "created": bool(new)}


def delete_memory(team_dir, agent: str, name: str, by: str = "user") -> dict:
    """Delete one of an agent's memories; a copy goes to the archive."""
    _guard()
    from . import memory_log
    store = _memory_store(team_dir, agent)
    try:
        content = store.read(name)
        if content is None:
            raise OperatorError(f"{agent} has no memory {name!r}")
        desc = next((e.description for e in store.index() if e.name == name), "")
        memory_log.archive(team_dir, agent, name, desc, content, reason="deleted", by=by)
        store.delete(name)
    finally:
        store.close()
    memory_log.record(team_dir, agent, "delete", name, by=by)
    return {"agent": agent, "name": name, "deleted": True, "archived": True}


def archived_memories(team_dir, agent: str) -> list[dict]:
    """What was deleted or merged away from an agent's memory, newest first."""
    from . import memory_log
    _memory_store(team_dir, agent).close()          # checks the agent
    return memory_log.archived(team_dir, agent)


def restore_memory(team_dir, agent: str, archive_id: str, by: str = "user") -> dict:
    """Put an archived memory back. Refused if a memory of that name exists
    now: read both and save the one you want instead."""
    _guard()
    from . import memory_log
    store = _memory_store(team_dir, agent)
    try:
        doc = next((d for d in memory_log.archived(team_dir, agent)
                    if d.get("id") == archive_id), None)
        if doc is None:
            raise OperatorError(f"nothing archived as {archive_id!r}")
        if store.read(doc["name"]) is not None:
            raise OperatorError(f"{agent} has a memory named {doc['name']!r} now; "
                                f"delete or rename it first")
        memory_log.take_archived(team_dir, agent, archive_id)
        _, why, body = _provenance(doc.get("content") or "")
        store.save(doc["name"], doc.get("description") or doc["name"], body or doc["content"],
                   why=why or f"restored by the operator ({by})")
    finally:
        store.close()
    memory_log.record(team_dir, agent, "save", doc["name"], new=True, by=by, restored=archive_id)
    return {"agent": agent, "name": doc["name"], "restored": True}


def _provenance(text: str):
    from .memory import extract_provenance
    return extract_provenance(text)


def review_memory(team_dir, agent: str, by: str = "user") -> dict:
    """Ask the supervisor driving the team to run a memory review for
    `agent` between its turns. Refused when no supervisor runs."""
    _guard()
    from . import distill_queue, heartbeat
    _memory_store(team_dir, agent).close()
    if not heartbeat.running(team_dir):
        raise OperatorError("no supervisor is running this team; a review is a turn, "
                            "so start the team first")
    rid = distill_queue.add(team_dir, "review", agent, by=by)
    return {"queued": rid, "agent": agent}


# --- bookmarks ------------------------------------------------------------------

def set_channel_bookmarks(team_dir, channel: str, bookmarks: list) -> list[dict]:
    """Replace a channel's bookmarks: [{title, url}] (a URL, a path, a go link)."""
    _guard()
    try:
        return channels_lib.set_bookmarks(team_dir, channel, bookmarks)
    except channels_lib.ChannelError as e:
        raise OperatorError(str(e)) from None


def set_task_links(team_dir, task_id: str, links: list) -> dict:
    """Replace a task's links: [{title, url}]."""
    _guard()
    from . import tasks
    try:
        t = tasks.update(team_dir, task_id, by="user", links=links)
    except tasks.TaskError as e:
        raise OperatorError(str(e)) from None
    if t is None:
        raise OperatorError(f"no task {task_id!r}")
    return t


# --- pictures and attachments ----------------------------------------------------

AVATARS = "avatars"
ATTACHMENTS = "attachments"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
AVATAR_BYTES = 2_000_000
ATTACHMENT_BYTES = 20_000_000


def avatars(team_dir) -> dict[str, str]:
    """{agent name or "user": file name} under <team_dir>/avatars/."""
    d = Path(team_dir) / AVATARS
    out = {}
    for f in sorted(d.iterdir()) if d.is_dir() else []:
        if f.suffix.lower() in IMAGE_TYPES:
            out[f.stem] = f.name
    return out


def set_avatar(team_dir, who: str, data: bytes | None, ext: str = ".png") -> dict:
    """Set (or, with no data, remove) the picture for an agent or for the
    operator ("user"). Kept as <team_dir>/avatars/<who><ext>."""
    _guard()
    from . import roster as roster_lib
    names = [a["name"] for a in roster_lib.load(Path(team_dir) / "roster.json").get("agents", [])]
    if who != "user" and who not in names:
        raise OperatorError(f"no teammate named {who!r}")
    ext = ext.lower() if ext.startswith(".") else "." + ext.lower()
    if data is not None and ext not in IMAGE_TYPES:
        raise OperatorError(f"a picture is one of {', '.join(sorted(IMAGE_TYPES))}")
    if data is not None and len(data) > AVATAR_BYTES:
        raise OperatorError(f"a picture is at most {AVATAR_BYTES // 1_000_000} MB")
    d = Path(team_dir) / AVATARS
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"{who}.*"):
        old.unlink()
    if data is None:
        return {"who": who, "removed": True}
    (d / f"{who}{ext}").write_bytes(data)
    return {"who": who, "file": f"{who}{ext}"}


def attach(team_dir, data: bytes, filename: str, by: str = "user") -> dict:
    """Keep a file the operator is sharing (a pasted screenshot, say) where
    the team can read it: <team_dir>/attachments/<time>-<name>. Returns its
    absolute path, which goes into the message so agents can open it."""
    _guard()
    import re
    import time
    if not data:
        raise OperatorError("nothing to attach")
    if len(data) > ATTACHMENT_BYTES:
        raise OperatorError(f"an attachment is at most {ATTACHMENT_BYTES // 1_000_000} MB")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(filename or "file").name)[-80:] or "file"
    d = Path(team_dir) / ATTACHMENTS
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
    n = 1
    while p.exists():
        p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}-{name}"
        n += 1
    p.write_bytes(data)
    return {"path": str(p.resolve()), "name": p.name, "bytes": len(data), "by": by}



def migrate_project_labels(team_dir, dry_run: bool = False) -> list[dict]:
    """Turn each project label into a parent task: one top-level task per
    label (level 1, a "project"), with the label's top-level tasks moved
    under it. A label whose tasks are all closed becomes a closed, archived
    parent. Labels that already have a parent of that title are reused.
    Returns what it did, or would do."""
    _guard()
    from . import tasks
    state = tasks.snapshot(team_dir)
    names = tasks.level_names(team_dir)
    existing = {(t.get("title") or "").strip().lower(): tid for tid, t in state.items()
                if not t.get("parent") and (t.get("level") == 1)}
    groups: dict[str, list[dict]] = {}
    for t in state.values():
        label = (t.get("project") or "").strip()
        if label and not t.get("parent") and (t.get("title") or "").strip().lower() != label.lower():
            groups.setdefault(label, []).append(t)
    out = []
    for label, members in sorted(groups.items()):
        closed = all(m.get("status") in tasks.TERMINAL for m in members)
        row = {"project": label, "tasks": len(members), "closed": closed,
               "parent": existing.get(label.lower())}
        out.append(row)
        if dry_run:
            continue
        if not row["parent"]:
            p = tasks.create(team_dir, label, label, created_by="user", level=1,
                             note=f"Made from the project label \u201c{label}\u201d.")
            row["parent"] = p["id"]
        for m in members:
            tasks.update(team_dir, m["id"], "user", parent=row["parent"])
        if closed:
            tasks.update(team_dir, row["parent"], "user", status="done", archived=True)
    _ = names
    return out
