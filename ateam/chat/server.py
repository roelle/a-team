"""a-team's server: the record over HTTP, operator actions, and a live feed.

    python -m ateam.chat                       # every team under ~/a-teams
    python -m ateam.chat --teams-root DIR      # or another root
    python -m ateam.chat --team-dir PATH       # or a team directory directly

Binds to 127.0.0.1 (or --host) and prints a URL with a one-time token
(--open also opens it); the browser keeps it as a cookie. Every API call needs it, every state-changing call must be
JSON (which a cross-site form cannot send), and the Host header must be a
name this server answers to (see Handler._local) -- this server can post as
you to a team of agents with shells, so a web page you happen to have open
must not be able to.

Reads go through ateam.record, changes through ateam.actions; nothing
here parses or writes the team's files itself.
"""
import argparse
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ateam import actions, scope, starter
from ateam import channels as channels_lib
from ateam import roster as roster_lib
from ateam.record import Feed, Record

from . import people
from .teams import Team, discover

STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "ateam"
UPLOAD_BYTES = 30_000_000        # a JSON body carrying a base64 picture or attachment
UPLOAD_PATHS = ("/api/attach", "/api/avatar")


class Forbidden(Exception):
    """Someone with a link of their own asked for what only the operator may."""


#: All someone other than the operator can reach, so a new endpoint starts closed.
PERSON_GET = {"/api/teams", "/api/state", "/api/messages", "/api/artifact", "/api/tasks",
              "/api/task", "/api/search", "/api/models", "/api/activity", "/api/wiki",
              "/api/events", "/api/file", "/api/avatar"}
PERSON_POST = {"/api/post", "/api/task", "/api/attach", "/api/bookmarks", "/api/message"}


def epoch(ts) -> float | None:
    """The record's timestamps carry no zone (or are UTC ISO, for tasks).
    The page is told the instant ("t"), so it can show any zone the person
    picks."""
    from ateam.record import instant
    return instant(ts)


def stamped(rows):
    for r in rows:
        if isinstance(r, dict) and r.get("ts") and "t" not in r:
            r["t"] = epoch(r["ts"])
    return rows


class App:
    def __init__(self, teams_root: Path | None, team_dirs: list[Path], author: str):
        self.teams_root = teams_root
        self.team_dirs = team_dirs
        self.owner = author
        self._request = threading.local()
        self.host = "127.0.0.1"         # what serve() bound; the Host check reads it
        # A restart for an update passes the token on, so open tabs stay
        # signed in. Taken out of the environment at once: supervisors this
        # app starts inherit it, and an agent with a shell must not be able
        # to post as the operator.
        self.token = os.environ.pop("ATEAM_TOKEN", "") or secrets.token_urlsafe(18)
        self._teams: dict[str, Team] = {}
        self.updating: dict = {"state": "idle", "log": []}
        self.argv: list[str] = []        # main's arguments, for a restart in place
        self.reexec = self._reexec
        self._usage_cache: dict = {}
        self.refresh()

    @property
    def person(self) -> str | None:
        """Who this request is from, when it is not the operator."""
        return getattr(self._request, "person", None)

    @person.setter
    def person(self, name: str | None) -> None:
        self._request.person = name

    @property
    def author(self) -> str:
        return self.person or self.owner

    def refresh(self) -> dict[str, Team]:
        self._teams = discover(self.teams_root, self.team_dirs)
        return self._teams

    def team(self, name: str) -> Team:
        t = self._teams.get(name) or self.refresh().get(name)
        if t is None:
            raise LookupError(f"no team {name!r}")
        return t

    # --- reads ------------------------------------------------------------------

    def teams(self) -> list[dict]:
        out = []
        for name, t in self.refresh().items():
            rec = Record(t.dir)
            out.append({"name": name, "dir": str(t.dir), "agents": rec.agents(),
                        "supervisor": bool(rec.supervisor()), "stopped": rec.stopped()})
        return out

    def state(self, team: str) -> dict:
        t = self.team(team)
        rec = Record(t.dir)
        presence = rec.presence()
        load = rec.load() if hasattr(rec, "load") else {}
        models = rec.models()
        agents = []
        for a in rec.roster().get("agents", []):
            n = a["name"]
            agents.append({"name": n, "alias": a.get("alias"), "role": a.get("role", ""),
                           "aliases": a.get("aliases") or [],
                           "presence": presence.get(n, {}), "model": models.get(n, {}),
                           "load": load.get(n)})
        sup = rec.supervisor()
        handles = self.my_handles(rec)
        # Keyed as the client keys its views ("c:<channel>", "d:<a>,<b>"):
        # when each last had something from someone else, and when an agent
        # last addressed the operator there (an @mention, or a direct
        # message). So what arrived while nobody was looking still shows.
        mentions, latest = {}, {}
        for m in rec.bus():
            frm = m.get("from")
            if not frm or frm == "user":
                continue
            if m.get("channel"):
                key = f"c:{m['channel']}"
            elif m.get("to"):
                key = "d:" + ",".join(sorted((frm, m["to"])))
            else:
                continue
            if frm == "supervisor" and m.get("to") != "user":
                continue
            latest[key] = m.get("ts")
            if frm != "supervisor" and (mentions_me(m, handles)
                                        or (not m.get("channel") and m.get("to") == "user")):
                mentions[key] = m.get("ts")
        return {"team": team, "author": self.author, "member": bool(self.person),
                "operator": rec.operator(),
                "mentions_of_user": mentions, "latest": latest,
                "my_handles": sorted(handles),
                "needs_you": rec.needs_you(),
                "agents": agents, "channels": rec.channels(),
                "conversations": rec.conversations(), "tasks": self.tasks(team)[:300],
                "supervisor": sup, "stopped": rec.stopped(),
                "reply_counts": rec.reply_counts(), "avatars": rec.avatars(),
                "task_levels": self.tree(t.dir).get("levels") or [],
                "app_version": _app_version(),
                "installed_version": _installed_version(),
                "server_tz": time.strftime("%Z")}

    def my_handles(self, rec: Record) -> set[str]:
        """What an @ that means you can say, lowercased: @user, the roster
        operator's handles, and the name you post as. Agents see that name on
        what you post and address you by it, roster operator or not."""
        op = rec.operator()
        names = {"user", op.get("name") or "", *(op.get("handles") or []), self.author or ""}
        return {n.lower() for n in names if roster_lib.is_handle(n)}

    TASK_FIELDS = ("id", "project", "title", "owner", "status", "note", "check_after", "until",
                   "requires_review", "updated_at", "created_at", "created_by", "evidence",
                   "channel", "rank", "deadline", "collaborators", "links",
                   "parent", "level", "priority", "auto_close", "archived",
                   "blocked_by", "writes", "thread", "origin")

    def tree(self, team_dir) -> dict:
        """Record.task_tree, recomputed only when the event or task log changed."""
        return self._cached(team_dir, "tree", lambda: Record(team_dir).task_tree(),
                            {"levels": [], "nodes": {}})

    def _cached(self, team_dir, what, compute, empty):
        key = [what]
        for f in ("events.jsonl", "tasks.jsonl", "roster.json"):
            try:
                st = (Path(team_dir) / f).stat()
                key.append((st.st_mtime_ns, st.st_size))
            except OSError:
                key.append(None)
        slot = f"{team_dir}|{what}"
        hit = self._usage_cache.get(slot)
        if hit and hit[0] == key:
            return hit[1]
        try:
            value = compute()
        except Exception:
            value = empty
        self._usage_cache[slot] = (key, value)
        return value

    def usage(self, team_dir) -> dict:
        """Record.task_usage, recomputed only when the event or task log changed."""
        key = []
        for f in ("events.jsonl", "tasks.jsonl"):
            try:
                st = (Path(team_dir) / f).stat()
                key.append((st.st_mtime_ns, st.st_size))
            except OSError:
                key.append(None)
        hit = self._usage_cache.get(str(team_dir))
        if hit and hit[0] == key:
            return hit[1]
        try:
            usage = Record(team_dir).task_usage()
        except Exception:
            usage = {}
        self._usage_cache[str(team_dir)] = (key, usage)
        return usage

    def tasks(self, team: str) -> list[dict]:
        t = self.team(team)
        rec = Record(t.dir)
        reviewed = {r.get("task_id") for r in rec.reviews()
                    if r.get("verdict") in ("approved", "waived") and r.get("task_id")}
        usage = self.usage(t.dir)
        nodes = self.tree(t.dir).get("nodes", {})
        return [{k: x.get(k) for k in self.TASK_FIELDS}
                | {"reviewed": x.get("id") in reviewed, "usage": usage.get(x.get("id")),
                   "node": nodes.get(x.get("id")),
                   "t_updated": epoch(x.get("updated_at")), "t_created": epoch(x.get("created_at"))}
                for x in rec.tasks()]

    def task(self, team: str, task_id: str) -> dict:
        """One task with everything about it: its log, its messages, its reviews."""
        from ateam import tasks as tasks_lib
        t = self.team(team)
        rec = Record(t.dir)
        row = next((x for x in self.tasks(team) if x["id"] == task_id), None)
        if row is None:
            raise LookupError(f"no task {task_id}")
        state = tasks_lib.snapshot(t.dir)
        return {"task": row, "overlaps": tasks_lib.overlaps(state, state[task_id]),
                "history": stamped([e for e in tasks_lib.read(t.dir) if e.get("id") == task_id]),
                "messages": stamped([m for m in rec.bus() if m.get("task_id") == task_id][-200:]),
                "reviews": stamped([r for r in rec.reviews() if r.get("task_id") == task_id])}

    def messages(self, team: str, q: dict) -> dict:
        rec = Record(self.team(team).dir)
        if q.get("thread"):
            return {"messages": stamped(rec.thread(q["thread"]))}
        if q.get("channel"):
            # A deleted channel's history stays in the record, out of sight.
            gone = (rec.channels().get(q["channel"]) or {}).get("deleted")
            rows = [] if gone else rec.messages(channel=q["channel"])
        elif q.get("dm"):
            a, _, b = q["dm"].partition(",")
            b = b or "user"
            rows = rec.messages(dm=(a, b), include_plumbing="supervisor" in (a, b))
        else:
            rows = []
        return {"messages": stamped(rows[-int(q.get("limit") or 500):]),
                "reply_counts": rec.reply_counts()}

    # --- pictures, attachments, bookmarks ---------------------------------------

    def attach(self, team: str, body: dict) -> dict:
        import base64
        try:
            data = base64.b64decode(body.get("data") or "", validate=True)
        except ValueError:
            raise actions.OperatorError("send the file as base64") from None
        return actions.attach(self.team(team).dir, data, body.get("name") or "file",
                              by=self.author)

    def avatar(self, team: str, body: dict) -> dict:
        import base64
        t = self.team(team)
        if not body.get("data"):
            return actions.set_avatar(t.dir, body.get("who") or "", None)
        try:
            data = base64.b64decode(body["data"], validate=True)
        except ValueError:
            raise actions.OperatorError("send the picture as base64") from None
        return actions.set_avatar(t.dir, body.get("who") or "", data, body.get("ext") or ".png")

    def bookmarks(self, team: str, body: dict) -> dict:
        t = self.team(team)
        if body.get("task"):
            return {"task": actions.set_task_links(t.dir, body["task"], body.get("bookmarks") or [])}
        if body.get("channel"):
            return {"bookmarks": actions.set_channel_bookmarks(t.dir, body["channel"],
                                                                body.get("bookmarks") or [])}
        raise actions.OperatorError("say which channel or task")

    # --- changes ------------------------------------------------------------------

    def post(self, team: str, body: dict) -> dict:
        t = self.team(team)
        content = (body.get("content") or "").strip()
        kind = body.get("kind") or "work"
        if body.get("channel"):
            return actions.post(t.dir, body["channel"], content, author=self.author,
                                thread=body.get("thread") or None, kind=kind)
        if body.get("to"):
            return {"result": actions.dm(t.dir, body["to"], content, author=self.author,
                                         thread=body.get("thread") or None, kind=kind)}
        raise actions.OperatorError("say where: a channel or an agent")

    def message(self, team: str, body: dict) -> dict:
        """Edit or delete a message you sent (actions.edit_message). Someone
        with a link of their own changes only what they sent."""
        t, mid = self.team(team), body.get("id") or ""
        own = bool(self.person)
        if body.get("action") == "edit":
            return actions.edit_message(t.dir, mid, body.get("content") or "",
                                        by=self.author, own_only=own)
        if body.get("action") == "delete":
            return actions.delete_message(t.dir, mid, by=self.author, own_only=own)
        raise actions.OperatorError("edit or delete")

    def clear_waiting(self, team: str, body: dict) -> dict:
        return {"cleared": actions.clear_waiting(self.team(team).dir, ids=body.get("ids") or (),
                                                 sender=body.get("from"), by=self.author)}

    def channel(self, team: str, body: dict) -> dict:
        t = self.team(team)
        action = body.get("action")
        if action == "create":
            return actions.create_channel(t.dir, body.get("name", ""), body.get("members") or [],
                                          body.get("topic", ""),
                                          everyone=bool(body.get("everyone")))
        if action == "members":
            return {"members": actions.set_members(t.dir, body["name"], body.get("add") or [],
                                                   body.get("remove") or [],
                                                   everyone=body.get("everyone"))}
        if action == "wake":
            return {"wake": actions.set_channel_wake(t.dir, body["name"], body.get("agents") or [])}
        if action == "topic":
            actions.set_topic(t.dir, body["name"], body.get("topic") or "")
            return {"ok": True}
        fn = {"archive": actions.archive_channel, "delete": actions.delete_channel,
              "restore": actions.restore_channel}.get(action)
        if fn:
            return fn(t.dir, body["name"])
        raise actions.OperatorError(f"unknown channel action {action!r}")

    def task_action(self, team: str, body: dict) -> dict:
        t = self.team(team)
        action = body.get("action")
        if self.person and action not in ("create", "update"):
            raise Forbidden(f"{action} is the operator's")
        if action == "create":
            return actions.create_task(
                t.dir, body.get("project") or "", body.get("title") or "",
                owner=body.get("owner") or None, note=body.get("note") or "",
                channel=body.get("channel") or None, status=body.get("status") or None,
                deadline=body.get("deadline") or None,
                requires_review=bool(body.get("requires_review")), rank=body.get("rank"),
                parent=body.get("parent") or None, level=body.get("level"),
                priority=body.get("priority"), auto_close=bool(body.get("auto_close")),
                thread=body.get("thread") or None, origin=body.get("origin") or None)
        if action == "update":
            fields = ("status", "owner", "title", "project", "note", "channel", "deadline", "rank",
                      "parent", "level", "priority", "auto_close", "archived",
                      "blocked_by", "writes")
            return actions.update_task(t.dir, body["id"],
                                       **{k: body[k] for k in fields if k in body})
        if action == "migrate_labels":
            return {"plan": actions.migrate_project_labels(t.dir, dry_run=bool(body.get("dry_run")))}
        if action == "waive":
            return actions.waive_review(t.dir, body["id"], by=self.author,
                                        reason=body.get("reason") or "waived on the board")
        raise actions.OperatorError(f"unknown task action {action!r}")

    def model(self, team: str, body: dict) -> dict:
        t = self.team(team)
        if not body.get("model"):
            return actions.clear_model(t.dir, body["agent"], by=self.author)
        return actions.set_model(t.dir, body["agent"], body["model"],
                                 until=body.get("until") or None,
                                 reason=body.get("reason") or "set from a-team", by=self.author)

    def setup(self) -> dict:
        """What a first run needs to know: this machine's runtimes, and where
        a new team would go."""
        return {"runtimes": starter.runtimes(), "teams_root": str(self.teams_root or "")}

    def new_team(self, body: dict) -> dict:
        """Create a team and start it. A team that made it but could not start
        is still made, and says why."""
        if not self.teams_root:
            raise actions.OperatorError("this app was started with --team-dir only; "
                                        "there is no teams root to add a team to")
        made = actions.create_team(self.teams_root, body.get("name") or "",
                                   body.get("runtime") or "")
        team = Team(Path(made["team_dir"]))
        try:
            started = team.start_supervisor()
        except Exception as e:
            started = {"started": False, "reason": str(e)}
        return {"team": team.name, "note": made["note"], "supervisor": started}

    def supervisor(self, team: str, body: dict) -> dict:
        t = self.team(team)
        if body.get("action") == "start":
            return t.start_supervisor()
        if body.get("action") == "stop":
            return t.stop_supervisor()
        if body.get("action") == "restart":
            return actions.restart(t.dir, now=bool(body.get("now")), by=self.author)
        raise actions.OperatorError("action is start, stop or restart")

    # --- admin --------------------------------------------------------------------

    def admin(self, team: str) -> dict:
        t = self.team(team)
        roster, rec = t.roster(), Record(t.dir)
        return {"agents": [{k: a.get(k) for k in ("name", *actions.AGENT_FIELDS) if k in a}
                           for a in roster.get("agents", [])],
                "fields": list(actions.AGENT_FIELDS),
                "memory_model": roster.get("memory_model"),
                "avatars": actions.avatars(t.dir),
                "norms": actions.norms(t.dir),
                "quiet_mail": rec.quiet_mail() if hasattr(rec, "quiet_mail") else None}

    def agent(self, team: str, body: dict) -> dict:
        t = self.team(team)
        action, name = body.get("action"), body.get("name") or ""
        fields = {k: body[k] for k in actions.AGENT_FIELDS if k in body and k != "role"}
        if action == "add":
            return actions.add_agent(t.dir, name, role=body.get("role") or "",
                                     channels="all" if body.get("all_channels", True) else None,
                                     **fields)
        if action == "update":
            if "role" in body:
                fields["role"] = body["role"]
            return actions.update_agent(t.dir, name, **fields)
        if action == "remove":
            return actions.remove_agent(t.dir, name)
        raise actions.OperatorError("action is add, update or remove")

    def norms(self, team: str, body: dict) -> dict:
        return actions.set_norms(self.team(team).dir, body.get("text") or "", by=self.author)

    def memories(self, team: str, agent: str) -> dict:
        """One agent's memories with their history, its archive, its recent
        memory reviews, and how recall has gone for it."""
        t = self.team(team)
        rec = Record(t.dir)
        content = {m["name"]: m["content"] for m in actions.memories(t.dir, agent)}
        stats = rec.memory_stats(agent)
        from ateam.memory import extract_provenance
        for m in stats:
            _, why, body = extract_provenance(content.get(m["name"], ""))
            m["content"], m["why"] = body, why or ""
        reviews = [e for e in rec.events("memory_review") if e.get("agent") == agent][-5:]
        queued = any(r.get("action") == "review" and r.get("agent") == agent
                     for r in _distill_queue().pending(t.dir))
        recall = rec.recall_stats(agent) if hasattr(rec, "recall_stats") else None
        return {"memories": stats, "archived": rec.memory_archive(agent),
                "reviews": list(reversed(reviews)), "review_queued": queued, "recall": recall}

    def memory(self, team: str, body: dict) -> dict:
        t = self.team(team)
        if body.get("action") == "restore":
            return actions.restore_memory(t.dir, body.get("agent") or "", body.get("id") or "",
                                          by=self.author)
        if body.get("action") == "review":
            return actions.review_memory(t.dir, body.get("agent") or "", by=self.author)
        if body.get("action") == "save":
            return actions.save_memory(t.dir, body.get("agent") or "", body.get("name") or "",
                                       body.get("content") or "", body.get("description") or "",
                                       by=self.author)
        if body.get("action") == "delete":
            return actions.delete_memory(t.dir, body.get("agent") or "", body.get("name") or "",
                                         by=self.author)
        raise actions.OperatorError("action is save, delete, restore or review")

    # --- update -------------------------------------------------------------------

    def versions(self) -> dict:
        from ateam import __version__
        teams = []
        for name, t in self.refresh().items():
            beat = Record(t.dir).supervisor() or {}
            teams.append({"name": name, "running": bool(beat), "version": beat.get("version"),
                          "restartable": bool(beat.get("restartable")),
                          "draining": bool(beat.get("draining")),
                          "inflight": beat.get("inflight") or [], "started": beat.get("started")})
        return {"version": __version__, "teams": teams,
                "update": {k: v for k, v in self.updating.items() if k != "log"},
                "log": self.updating.get("log", [])[-200:]}

    def update(self, team: str, body: dict) -> dict:
        from . import update as up
        if body.get("action") == "check":
            try:
                return up.plan_self()
            except Exception as e:
                return {"error": str(e)}
        if body.get("action") == "restart":
            # The installed code is newer than this process: an upgrade
            # landed under a running server, and the page, read from disk,
            # already calls what only the new code answers.
            threading.Timer(0.5, self.reexec).start()
            return {"restarting": True}
        if body.get("action") != "run":
            raise actions.OperatorError("action is check, run or restart")
        if self.updating.get("state") == "running":
            raise actions.OperatorError("an update is already running")
        self.updating = {"state": "running", "log": [],
                         "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        threading.Thread(target=self._run_update, args=(bool(body.get("now")),),
                         daemon=True).start()
        return {"started": True}

    def _run_update(self, now: bool) -> None:
        from . import update as up
        log = self.updating["log"].append
        try:
            out = up.run([t.dir for t in self.refresh().values()], now=now, log=log)
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        if out.get("error"):
            log(f"error: {out['error']}")
            self.updating.update(state="error", error=out["error"])
            return
        log("restarting a-team…")
        self.updating.update(state="restarting", result=out)
        time.sleep(1.5)                  # let the page see "restarting" first
        self.reexec()

    def restart_args(self) -> list[str]:
        """The page reloads itself, so a restart opens no second browser tab."""
        return [a for a in self.argv if a not in ("--open", "--no-open")] + ["--no-open"]

    def _reexec(self) -> None:
        """Replace this process with a fresh one on the new code: same
        arguments (less --open), same token, same port."""
        os.environ["ATEAM_TOKEN"] = self.token
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, "-m", "ateam.chat", *self.restart_args()])


def _app_version() -> str:
    from . import __version__
    return __version__


_INSTALLED: dict = {}


def _installed_version() -> str | None:
    """The version on disk now, which an upgrade under a running server
    changes while _app_version() stays what this process started on."""
    init = Path(__file__).resolve().parent.parent / "__init__.py"
    try:
        mtime = init.stat().st_mtime
        if _INSTALLED.get("mtime") != mtime:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init.read_text(encoding="utf-8"), re.M)
            _INSTALLED.update(mtime=mtime, version=m.group(1) if m else None)
    except OSError:
        return None
    return _INSTALLED.get("version")


def mentions_me(m: dict, handles: set[str]) -> bool:
    """Whether a message mentions you: recorded so when it was posted, or an
    @ of one of your handles in it. The text is read too, because what was
    recorded depends on the code the agent posted with and the roster then."""
    if "user" in (m.get("mentions") or []):
        return True
    return any(h.lower() in handles for h in channels_lib.handles_in(m.get("content") or ""))


def _distill_queue():
    from ateam import distill_queue
    return distill_queue


class Handler(BaseHTTPRequestHandler):
    app: App = None                  # set by serve()
    server_version = "a-team"

    def log_message(self, fmt, *args):  # quiet; errors still go to stderr
        pass

    # --- plumbing ------------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, p: Path):
        """A file's bytes, for <img> and the like. A file here may have been
        written by an agent: the sandbox keeps an SVG opened in a tab from
        running script as you."""
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        try:
            data = p.read_bytes()
        except OSError:
            return self._json({"error": "unreadable"}, 404)
        return self._send(200, data, ctype, {"Content-Security-Policy": "sandbox; script-src 'none'",
                                             "Content-Disposition": "inline"})

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _local(self) -> bool:
        """Is the Host header one this server answers to?

        The check stops DNS rebinding: a page on evil.example whose name is
        made to resolve here sends Host: evil.example. Loopback names always
        pass; so does the name given to --host. Bound beyond loopback, so
        does any IP literal, which no rebinding attack can produce.
        """
        raw = self.headers.get("Host") or ""
        host = raw[1:raw.index("]")] if raw.startswith("[") and "]" in raw \
            else raw.rsplit(":", 1)[0]
        host = host.lower()
        if host in ("127.0.0.1", "localhost", "::1") or host == self.app.host.lower():
            return True
        if is_loopback(self.app.host):
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def _is_operator(self, token: str) -> bool:
        # As bytes: compare_digest raises on a str that is not ASCII.
        return secrets.compare_digest(token.encode("utf-8", "replace"), self.app.token.encode())

    def _authed(self) -> bool:
        """Is this the operator, or someone with a link? Sets app.person, for
        this request's thread, to which; a thread may serve an earlier request."""
        self.app.person = None
        c = SimpleCookie(self.headers.get("Cookie") or "")
        given = [c[COOKIE].value] if COOKIE in c else []
        given.append(self.headers.get("X-Token", ""))
        if any(map(self._is_operator, given)):
            return True
        self.app.person = next(filter(None, map(people.who, given)), None)
        return self.app.person is not None

    def _query(self) -> tuple[str, dict]:
        u = urlparse(self.path)
        return u.path, {k: v[-1] for k, v in parse_qs(u.query).items()}

    # --- routes ----------------------------------------------------------------------

    def do_GET(self):
        if not self._local():
            return self._send(403, b"local only", "text/plain")
        path, q = self._query()
        if path in ("/", "/index.html", "/wiki", "/board", "/admin"):
            extra = {}
            if q.get("token") and (self._is_operator(q["token"]) or people.who(q["token"])):
                extra["Set-Cookie"] = f"{COOKIE}={q['token']}; Path=/; HttpOnly; SameSite=Strict"
            elif not self._authed():
                return self._send(401, b"Open the URL a-team printed (it carries the token).",
                                  "text/plain")
            page = {"/wiki": "wiki.html", "/board": "board.html",
                    "/admin": "admin.html"}.get(path, "index.html")
            return self._send(200, (STATIC / page).read_bytes(), "text/html; charset=utf-8", extra)
        if path == "/favicon.ico":          # what browsers ask for unprompted
            return self._send(200, (STATIC / "favicon.svg").read_bytes(), "image/svg+xml")
        if path.startswith("/static/"):
            f = (STATIC / path[len("/static/"):]).resolve()
            if STATIC not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            return self._send(200, f.read_bytes(), ctype)
        if not path.startswith("/api/"):
            return self._send(404, b"not found", "text/plain")
        if not self._authed():
            return self._json({"error": "not authorised"}, 401)
        if self.app.person and path not in PERSON_GET:
            return self._json({"error": "that is the operator's"}, 403)
        try:
            if path == "/api/setup":
                return self._json(self.app.setup())
            if path == "/api/teams":
                return self._json({"teams": self.app.teams()})
            team = q.get("team", "")
            if path == "/api/state":
                return self._json(self.app.state(team))
            if path == "/api/messages":
                return self._json(self.app.messages(team, q))
            if path == "/api/artifact":
                return self._json(self.app.team(team).read_artifact(q.get("path", ""),
                                                                    not self.app.person))
            if path == "/api/tasks":
                return self._json({"tasks": self.app.tasks(team)})
            if path == "/api/task":
                return self._json(self.app.task(team, q.get("id", "")))
            if path == "/api/search":
                return self._json({"hits": Record(self.app.team(team).dir).search(q.get("q", ""))})
            if path == "/api/models":
                # The team's runner -- its roster's supervisor block, as
                # the supervisor Start launches would use -- not this app's
                # environment, which named no runner and listed nothing.
                from ateam import runner as runner_lib
                td = self.app.team(team).dir
                cls = runner_lib.runner_class(team_dir=td)
                return self._json({"runner": getattr(cls, "label", None),
                                   "models": runner_lib.list_models(team_dir=td)})
            if path == "/api/activity":
                rows = [e for e in Record(self.app.team(team).dir).events()
                        if e.get("agent") == q.get("agent")]
                return self._json({"events": stamped(rows[-120:])})
            if path == "/api/wiki":
                t = self.app.team(team)
                return self._json({"norms": Record(t.dir).norms(), "memories": t.memories()})
            if path == "/api/events":
                return self._events(team)
            if path == "/api/file":
                p, why = self.app.team(team).resolve(q.get("path", ""), not self.app.person)
                if p is None or not p.is_file():
                    return self._json({"error": why or "no such file"}, 404)
                return self._raw(p)
            if path == "/api/avatar":
                t = self.app.team(team)
                f = actions.avatars(t.dir).get(q.get("who", ""))
                if not f:
                    return self._json({"error": "no picture"}, 404)
                return self._raw(t.dir / actions.AVATARS / f)
            if path == "/api/admin":
                return self._json(self.app.admin(team))
            if path == "/api/memories":
                return self._json(self.app.memories(team, q.get("agent", "")))
            if path == "/api/norm_candidates":
                return self._json({"groups": Record(self.app.team(team).dir).norm_candidates()})
            if path == "/api/versions":
                return self._json(self.app.versions())
        except actions.OperatorError as e:
            return self._json({"error": str(e)}, 400)
        except LookupError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        if not self._local() or not self._authed():
            return self._json({"error": "not authorised"}, 401)
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json({"error": "send JSON"}, 415)
        path, q = self._query()
        if self.app.person and path not in PERSON_POST:
            return self._json({"error": "that is the operator's"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            cap = UPLOAD_BYTES if path in UPLOAD_PATHS else 1_000_000
            if length > cap:
                return self._json({"error": f"too large (at most {cap // 1_000_000} MB)"}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            team = body.get("team") or q.get("team", "")
            route = {"/api/post": self.app.post, "/api/channel": self.app.channel,
                     "/api/model": self.app.model, "/api/supervisor": self.app.supervisor,
                     "/api/task": self.app.task_action, "/api/agent": self.app.agent,
                     "/api/norms": self.app.norms, "/api/memory": self.app.memory,
                     "/api/update": self.app.update, "/api/attach": self.app.attach,
                     "/api/avatar": self.app.avatar, "/api/bookmarks": self.app.bookmarks,
                     "/api/clear_waiting": self.app.clear_waiting,
                     "/api/message": self.app.message}
            if path == "/api/new_team":
                return self._json(self.app.new_team(body))
            fn = route.get(path)
            if fn is None:
                return self._json({"error": "unknown endpoint"}, 404)
            return self._json(fn(team, body))
        except actions.OperatorError as e:
            return self._json({"error": str(e)}, 400)
        except Forbidden as e:
            return self._json({"error": str(e)}, 403)
        except LookupError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _events(self, team: str):
        """Server-sent events: each row appended to the record, as it lands."""
        t = self.app.team(team)
        feed = Feed(t.dir)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_beat = 0.0
        try:
            while True:
                items = feed.poll()
                for it in items:
                    if isinstance(it, dict) and isinstance(it.get("item"), dict):
                        stamped([it["item"]])
                    self.wfile.write(b"data: " + json.dumps(it, default=str).encode() + b"\n\n")
                now = time.monotonic()
                if now - last_beat > 15:
                    self.wfile.write(b": keepalive\n\n")
                    last_beat = now
                self.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def _stop(signum, frame):
    raise KeyboardInterrupt


def start_teams(app: App) -> list[str]:
    """Start every team except those stopped on purpose; what happened to each."""
    out = []
    for name, team in app.refresh().items():
        why = Record(team.dir).stopped()
        if why:
            out.append(f"{name} stays stopped: {why}")
            continue
        try:
            done = team.start_supervisor()
            out.append(f"{name} started" if done.get("started")
                       else f"{name}: {done.get('reason')}")
        except Exception as e:
            out.append(f"{name} did not start: {e}")
    return out


def opens_browser(asked, host: str) -> bool:
    """--open or --no-open, or else: is someone at this machine's own screen?"""
    if asked is not None:
        return asked
    return is_loopback(host) and sys.stdout.isatty() and not os.environ.get("SSH_CONNECTION")


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    Handler.app = app
    app.host = host
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    httpd = Server((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["people"]:
        return people.main(args[1:])
    ap = argparse.ArgumentParser(prog="a-team", description=__doc__.split("\n\n")[0])
    from . import __version__
    ap.add_argument("--version", action="version",
                    version=f"a-team {__version__}")
    ap.add_argument("--teams-root", default=os.environ.get("ATEAM_TEAMS_ROOT") or scope.default_teams_root())
    ap.add_argument("--team-dir", action="append", default=[],
                    help="a team directory to show (repeatable)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1). Anything "
                         "beyond loopback is plain HTTP: anyone who can reach "
                         "it and has the printed URL can post as you")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action=argparse.BooleanOptionalAction, default=None,
                    help="open the URL in a browser (default: yes when you start "
                         "it yourself on this machine, not over SSH or as a service)")
    ap.add_argument("--start-teams", action="store_true",
                    help="start each team that was not stopped on purpose, for "
                         "when this is what comes up after a restart")
    ap.add_argument("--author", default=os.environ.get("USER") or "operator",
                    help="your name on what you post")
    a = ap.parse_args(argv)
    if os.environ.get("ATEAM_AGENT"):
        sys.exit("a-team speaks as the operator; it will not run inside an agent's session")
    app = App(Path(a.teams_root).expanduser(), [Path(p) for p in a.team_dir], a.author)
    app.argv = list(argv if argv is not None else sys.argv[1:])
    httpd = serve(app, a.host, a.port)
    names = ", ".join(app.refresh()) or "none found"
    shown = "127.0.0.1" if a.host in ("0.0.0.0", "::", "") else a.host
    url = f"http://{'[' + shown + ']' if ':' in shown else shown}:{httpd.server_address[1]}/?token={app.token}"
    print(f"a-team: {len(app.refresh())} team(s) ({names})\n"
          f"open  {url}", flush=True)
    if not is_loopback(a.host):
        print(f"a-team: listening on {a.host}, beyond this machine, over plain "
              f"HTTP. Anyone who can reach it and has that URL can post as "
              f"{a.author} to agents with shells.", file=sys.stderr, flush=True)
    # As PID 1 in a container, Python ignores SIGTERM unless told otherwise.
    signal.signal(signal.SIGTERM, _stop)
    if a.start_teams:
        for line in start_teams(app):
            print(f"a-team: {line}", flush=True)
    if opens_browser(a.open, a.host):
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
