"""Memories and norms that come up before the call they are about.

## Why this file exists

Asked for: "For a lot of tool use, there are memories associated with the
tool: gotchas, etc. I'm seeing those get loaded in every wake. Could we
assign memories to tools and always, and only, load them before using the
tool?" And: "make sure the rules get in after any compaction and don't get
diluted for long-lived agents."

A gotcha about a complicated tool was either recalled on every wake, where
it cost tokens and was far from the call it was about, or not at all, and
the agent made the mistake, read the error, and tried again, several times.
So a memory, or a section of the team's NORMS.md, can name its triggers:
the calls it is about. The first matching call is held, and the agent is
shown what it has on that call and asked to make it again; the second goes
through. A memory with triggers is not recalled on a wake: it comes up when
it is needed.

## Triggers

`Tool` or `Tool(pattern)`, as Claude Code writes permission rules, several
separated by ";":

    Bash(kubectl *)             a shell command, or any part of one
                                (`cd x && kubectl apply` matches)
    Bash(git push)              no wildcard: that command, with any arguments
    send_to_teammate(user)      writing to the operator; (*) any teammate
    create_task(web*)           a task in a matching project; claim_task,
                                update_task and complete_task match on the
                                task's project
    Edit(*.ipynb)               a file tool, on a matching path
    mcp__github__*              any call to that server's tools

`Bash` (or `shell`) is every host's shell tool, whatever it is called there;
a team tool matches however a host prefixes it. Other names match as
written, with wildcards, and a pattern is matched against the call's
arguments.

## Staying in front of a long-lived agent

What was shown is kept per agent in <team_dir>/triggers/<agent>.json, and
shown again when it may no longer be in the agent's context: a new
conversation; a compaction (a runner that sees one says so, rearm(); else
the context is found to have shrunk by half); the context grown by
`rearm_tokens` since it was shown; or `rearm_hours` passed. A memory or a
norm that changes is new, and shown at its next call.

## Where a call can be held

Claude (the runner's pre-tool hook), the Antigravity SDK, OpenCode over ACP
(its policy plugin), any host running `ateam.hook_pre_tool_use`, and the
team's own tools on every host (mcp_base). Where nothing can stop a call
before it runs (agy's and Codex's own tools), what it triggered is a note at
the next step instead (steer.count_call): after the first use, before the
second.

## Triggers nobody wrote

Which memory comes up before which call is also learned, and every trigger,
written or learned, is judged by what happened after it was shown
(ateam/trigger_learn.py). A memory that would have made a failed call go
right gains a trigger for such calls; a trigger that keeps being shown
before calls it does not bear on stops being shown. memories() below is
where both take effect.

Roster: "memory_triggers": false turns it off; {"rearm_tokens": 100000,
"rearm_hours": 6, "max_chars": 6000, "learn": true, "show_above": 0.3,
"decay": 0.9, "judge_max": 3} are the defaults.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import filelock

DIR = "triggers"
DEFAULTS = {"rearm_tokens": 100_000, "rearm_hours": 6.0, "max_chars": 6000,
            # ateam/trigger_learn.py: learn triggers and judge them afterwards;
            # show one while its recent record helps this share of the time;
            # how much of the old record each new verdict keeps; and how many
            # questions a turn's record may put to the judge.
            "learn": True, "show_above": 0.3, "decay": 0.9, "judge_max": 3}
#: A context this much smaller than the last one seen was compacted.
COMPACTED_BELOW = 0.5
#: How long a process keeps an agent's triggered memories before reading
#: them again; a hook process lives for one call, a runner for days.
CACHE_S = 10.0

SHELL = {"bash", "shell", "run_command", "run_shell_command", "exec_command",
         "local_shell", "shell_command", "execute_command", "terminal"}
SERVERS = ("bus", "tasks", "memory", "self")
TASK_TOOLS = {"claim_task", "update_task", "complete_task"}
PATH_KEYS = ("file_path", "path", "notebook_path", "AbsolutePath", "TargetFile",
             "filePath", "target_file")
_HEAD = re.compile(r"^\s*-?\s*triggers\s*:\s*(.*)$", re.IGNORECASE)
_ONE = re.compile(r"^([A-Za-z0-9_.*?\[\]-]+)\s*(?:\((.*)\))?$")
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUB = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
#: Words in front of a command that are not the command.
WRAPPERS = {"sudo", "time", "nohup", "env", "command", "exec"}
#: Commands a shell line is never about.
TRIVIAL = {"cd", "export", "unset", "source", ".", "set", "echo", "true", "sleep",
           "pushd", "popd", "printf", "wait"}
#: Commands whose second word is a file or an expression, not a subcommand.
NO_SUB = {"cat", "ls", "grep", "rg", "sed", "awk", "find", "rm", "cp", "mv", "mkdir",
          "touch", "head", "tail", "python", "python3", "bash", "sh", "zsh", "node",
          "pytest", "ruby", "perl", "less", "wc", "chmod", "chown", "tar", "curl",
          "wget", "diff", "test", "which", "tee", "xargs", "sort", "jq"}


class TriggerError(ValueError):
    """A trigger that cannot be matched, phrased as a sentence."""


# --- what a trigger is -------------------------------------------------------------

def parse(raw) -> list[str]:
    """Triggers from 'A; B(c)' or a list, checked. [] for none or 'none'."""
    if raw is None:
        return []
    parts = raw if isinstance(raw, list) else _split(str(raw))
    out = []
    for p in parts:
        p = " ".join(str(p).split())
        if not p or p.lower() == "none":
            continue
        if not _ONE.match(p):
            raise TriggerError(
                f"trigger {p!r} is not a call: write a tool name, with what it is "
                f"called on in parentheses, like 'Bash(kubectl *)', "
                f"'send_to_teammate(user)' or 'create_task(web*)'")
        out.append(p)
    return list(dict.fromkeys(out))


def _split(text: str) -> list[str]:
    """On ';' and newlines, and on ',' outside parentheses."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in ";\n" or (ch == "," and depth == 0):
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [s.strip() for s in out + [cur] if s.strip()]


def of(text: str) -> list[str]:
    """The triggers a memory's text names in its header, or []."""
    for i, line in enumerate((text or "").splitlines()[:12]):
        m = _HEAD.match(line)
        if m:
            try:
                return parse(m.group(1))
            except TriggerError:
                return []
    return []


def strip(text: str) -> str:
    """`text` without its triggers line."""
    lines = (text or "").splitlines()
    kept = [l for i, l in enumerate(lines) if not (i < 12 and _HEAD.match(l))]
    return "\n".join(kept).strip("\n")


def with_triggers(body: str, triggers: list[str]) -> str:
    """A memory's content carrying `triggers` as its first line."""
    body = strip(body).strip()
    if not triggers:
        return body
    return f"- Triggers: {'; '.join(triggers)}\n\n{body}"


# --- matching a call -----------------------------------------------------------------

def unwrap(tool: str, args: dict) -> tuple[str, dict]:
    """The call a host's generic MCP-call tool is making, or the call as it
    was given. Such a host has one tool for every MCP server and names the
    real one in its arguments ({"ServerName", "ToolName", "Arguments"}), so
    a trigger on save_memory or send_to_teammate(user) has to see through
    it, as tools_off does (policy.check_tool_policy)."""
    args = args if isinstance(args, dict) else {}
    inner = next((args[k] for k in ("ToolName", "tool_name", "toolName")
                  if isinstance(args.get(k), str) and args[k]), None)
    if not inner:
        return tool, args
    for k in ("Arguments", "arguments", "args"):
        v = args.get(k)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        if isinstance(v, dict):
            return inner, v
    return inner, {}


def tool_name(raw: str) -> str:
    """A-team's name for a call: a team tool however a host prefixes it."""
    name = str(raw or "")
    if "__" in name:
        name = name.rsplit("__", 1)[-1]
    for s in SERVERS:
        for prefix in (f"ateam_{s}_", f"ateam_{s}."):
            if name.startswith(prefix):
                return name[len(prefix):]
    return name


def _strings(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [s for x in v for s in _strings(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [str(v)]
    return []


def command(args: dict) -> str:
    """The command line of a shell call."""
    args = args or {}
    return " ".join(_strings(args.get("command") or args.get("cmd")
                             or args.get("CommandLine") or "")).strip()


def bare(part: str, named: bool = False) -> str:
    """One command of a shell line without what stands in front of it:
    'kubectl apply' of 'sudo KUBECONFIG=x kubectl apply'. With `named`,
    without the directory it was run from either: 'python -m pytest' of
    '.venv/bin/python -m pytest'."""
    words = part.split()
    while words and (words[0] in WRAPPERS or _ASSIGN.match(words[0])):
        words.pop(0)
    if named and words and "/" in words[0]:
        words[0] = words[0].rstrip("/").rsplit("/", 1)[-1] or words[0]
    return " ".join(words)


def head(cmd: str, sub: bool = True) -> str:
    """The command a shell line is about: 'git push' of 'cd repo && git
    push origin main', 'python -m pytest' of 'python -m pytest -q'; with
    sub=False the program alone, 'git' and 'python', wherever it was run
    from."""
    for part in _SPLIT.split(cmd or ""):
        words = bare(part, named=True).split()
        if not words or words[0] in TRIVIAL:
            continue
        if sub and len(words) > 2 and words[1] == "-m":
            return " ".join(words[:3])
        if sub and len(words) > 1 and words[0] not in NO_SUB and _SUB.match(words[1]):
            return f"{words[0]} {words[1]}"
        return words[0]
    return ""


def shape(tool: str, args: dict, sub: bool = True) -> str:
    """The kind of call this is, written as a trigger that matches it and
    the next one like it: 'Bash(git push)', 'Edit(*.ipynb)',
    'send_to_teammate(user)'. With sub=False a shell call is its command
    alone ('Bash(git)'): what a second try shares with the first."""
    name, args = tool_name(tool), args or {}
    if name.lower() in SHELL:
        h = head(command(args), sub)
        return f"Bash({h})" if h else ""
    if name == "send_to_teammate" or name in ("create_task", *TASK_TOOLS):
        on = next((s for s in subjects(tool, args) if s), "")
        return f"{name}({on})" if on else name
    paths = [s for k in PATH_KEYS for s in _strings(args.get(k))]
    ext = Path(paths[0]).suffix if paths else ""
    return f"{name}(*{ext})" if ext else name


def subjects(tool: str, args: dict, team_dir=None) -> list[str]:
    """What a pattern is matched against, for this call."""
    name, args = tool_name(tool).lower(), args or {}
    if name in SHELL:
        cmd = command(args)
        parts = [s for s in _SPLIT.split(cmd) if s.strip()]
        return list(dict.fromkeys([cmd, *parts, *(bare(p) for p in parts),
                                   *(bare(p, named=True) for p in parts)]))
    if name == "send_to_teammate":
        return [n.strip() for s in _strings(args.get("to")) for n in s.split(",") if n.strip()]
    if name in ("create_task", *TASK_TOOLS):
        out = _strings(args.get("project"))
        if args.get("task_id") and team_dir:
            try:
                from . import tasks as tasks_lib
                t = tasks_lib.get(team_dir, str(args["task_id"])) or {}
                out.append(t.get("project") or "")
            except Exception:
                pass
        return out or [""]
    paths = [s for k in PATH_KEYS for s in _strings(args.get(k))]
    return paths or _strings(args)


def _matches_tool(want: str, tool: str) -> bool:
    want, name, raw = want.lower(), tool_name(tool).lower(), str(tool).lower()
    if want in SHELL:
        return name in SHELL
    return fnmatch.fnmatchcase(name, want) or fnmatch.fnmatchcase(raw, want)


def _matches_subject(pattern: str, subject: str) -> bool:
    p, s = pattern.strip().lower(), " ".join(subject.split()).lower()
    if any(c in p for c in "*?["):
        return fnmatch.fnmatchcase(s, p) or fnmatch.fnmatchcase(Path(s).name, p)
    return s == p or s.startswith(p + " ")


def matches(trigger: str, tool: str, args: dict, team_dir=None) -> bool:
    m = _ONE.match(trigger.strip())
    if not m or not _matches_tool(m.group(1), tool):
        return False
    pattern = m.group(2)
    if pattern is None or pattern.strip() in ("", "*"):
        return True
    return any(_matches_subject(pattern, s) for s in subjects(tool, args, team_dir))


# --- what has triggers -------------------------------------------------------------

_cache: dict[tuple, tuple[float, list[dict]]] = {}


def config(team_dir) -> dict | None:
    """The roster's "memory_triggers" settings, or None when turned off."""
    try:
        doc = json.loads((Path(team_dir) / "roster.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    v = doc.get("memory_triggers", (doc.get("policy") or {}).get("memory_triggers", True))
    if v is False:
        return None
    return {**DEFAULTS, **(v if isinstance(v, dict) else {})}


def norms(team_dir) -> list[dict]:
    """Sections of NORMS.md that name triggers: {key, title, body, triggers}."""
    try:
        text = (Path(team_dir) / "NORMS.md").read_text(encoding="utf-8")
    except OSError:
        return []
    out, sections = [], re.split(r"(?m)^(?=#{1,6}\s)", text)
    for sec in sections:
        lines = sec.strip("\n").splitlines()
        if not lines or not lines[0].startswith("#"):
            continue
        trig = of("\n".join(lines[1:]))
        if trig:
            title = lines[0].lstrip("#").strip()
            body = strip("\n".join(lines[1:])).strip()
            out.append({"key": "norm:" + _digest(title + body), "kind": "norm",
                        "title": title, "body": body, "triggers": trig})
    return out


def memories(team_dir, agent: str) -> list[dict]:
    """The agent's own memories that come up before a call: the triggers
    each one names, less those found not to bear on their calls, and the
    ones learned for it (ateam/trigger_learn.py)."""
    from . import memory_log, trigger_learn
    from .memory import extract_provenance
    try:
        store = memory_log.open_store(team_dir, agent)
    except Exception:
        return []
    learned, muted = trigger_learn.standing(team_dir, agent)
    out = []
    try:
        for e in store.index():
            raw = store.read(e.name) or ""
            written = of(raw)
            trig = [t for t in written if (e.name, t) not in muted]
            trig += [t for t in learned.get(e.name, []) if t not in written]
            if trig:
                _, _, body = extract_provenance(raw)
                body = strip(body).strip()
                out.append({"key": f"memory:{e.name}:" + _digest(body), "kind": "memory",
                            "title": e.name, "body": body, "triggers": trig})
    finally:
        store.close()
    return out


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


def _sources(team_dir, agent: str) -> list[dict]:
    key = (str(team_dir), agent)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    found = memories(team_dir, agent) + norms(team_dir)
    _cache[key] = (time.monotonic(), found)
    return found


def forget_cache() -> None:
    _cache.clear()


def due_for(team_dir, agent: str, tool: str, args: dict) -> list[dict]:
    """Memories and norms whose triggers match this call (shown or not),
    each with the trigger that matched as "via"."""
    out = []
    calls = [unwrap(tool, args), (tool, args)]      # what it calls, and the call as made
    for s in _sources(team_dir, agent):
        via = next((t for t in s["triggers"]
                    if any(matches(t, name, a, team_dir) for name, a in calls)), None)
        if via:
            out.append({**s, "via": via})
    return out


# --- what has been shown -------------------------------------------------------------

def _path(team_dir, agent: str) -> Path:
    return Path(team_dir) / DIR / f"{agent}.json"


def _conversation(team_dir, agent: str) -> str:
    try:
        doc = json.loads((Path(team_dir) / "conversations.json").read_text(encoding="utf-8"))
        return str(doc.get(agent) or "")
    except (OSError, ValueError, AttributeError):
        return ""


def context_tokens(team_dir, agent: str, tail_bytes: int = 262_144) -> int | None:
    """How full the agent's context was at its last turn, if a runner said."""
    path = Path(team_dir) / "events.jsonl"
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if f'"{agent}"' not in line or '"turn"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "turn" and ev.get("agent") == agent:
            v = ev.get("context_tokens", ev.get("input_tokens"))
            return int(v) if isinstance(v, (int, float)) else None
    return None


def rearm(team_dir, agent: str, why: str = "") -> None:
    """Everything triggered is shown again at its next call: a runner saw
    the conversation compacted. Never raises."""
    try:
        path = _path(team_dir, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.locked(path):
            state = _load(path)
            state["shown"] = {}
            state["rearmed"] = {"ts": time.time(), "why": why}
            _save(path, state)
    except Exception:
        pass


def rearm_some(team_dir, agent: str, keys: list[str]) -> None:
    """These are shown again at their next call: a call they are about went
    wrong with them out of sight. Never raises."""
    try:
        path = _path(team_dir, agent)
        with filelock.locked(path):
            state = _load(path)
            shown = state.get("shown") or {}
            if any(shown.pop(k, None) for k in keys):
                _save(path, state)
    except Exception:
        pass


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def check(team_dir, agent: str, tool: str, args: dict, now: float | None = None) -> str:
    """What `agent` should have in front of it before this call and may
    not: the text to show, now marked shown; "" when there is nothing.
    Never raises: a problem here means the call goes ahead."""
    try:
        if not agent or not team_dir:
            return ""
        cfg = config(team_dir)
        if cfg is None:
            return ""
        hits = due_for(team_dir, agent, tool, args or {})
        if not hits:
            return ""
        now = time.time() if now is None else now
        conv, ctx = _conversation(team_dir, agent), context_tokens(team_dir, agent)
        path = _path(team_dir, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        with filelock.locked(path):
            state = _load(path)
            if state.get("conversation") != conv:
                state = {"conversation": conv, "shown": {}}
            last = state.get("context")
            if ctx is not None and isinstance(last, int) and ctx < last * COMPACTED_BELOW:
                state["shown"] = {}         # compacted since: none of it is there
            if ctx is not None:
                state["context"] = ctx
            shown = state.setdefault("shown", {})
            due = []
            for h in hits:
                seen = shown.get(h["key"])
                if seen and now - seen.get("ts", 0) < cfg["rearm_hours"] * 3600 and not (
                        ctx is not None and isinstance(seen.get("context"), int)
                        and ctx - seen["context"] >= cfg["rearm_tokens"]):
                    continue
                due.append(h)
                shown[h["key"]] = {"ts": now, "context": ctx}
            if not due:
                return ""
            _save(path, state)
        _record(team_dir, agent, tool, args or {}, due)
        return render(due, tool_name(tool), cfg["max_chars"])
    except Exception:
        return ""


def render(items: list[dict], tool: str, max_chars: int = DEFAULTS["max_chars"]) -> str:
    each = max(400, max_chars // max(1, len(items)))
    parts = []
    for it in items:
        body = it["body"]
        if len(body) > each:
            more = (f"read_memory('{it['title']}') for the rest" if it["kind"] == "memory"
                    else "NORMS.md has the rest")
            body = body[:each].rstrip() + f"\n[... {more}]"
        label = (f"Your memory '{it['title']}'" if it["kind"] == "memory"
                 else f"Team norm: {it['title']}")
        parts.append(f"## {label}\n(triggers: {'; '.join(it['triggers'])})\n\n{body}")
    return "\n\n".join(parts)


HOLD = ("[held: this {tool} call did not run. Here is what you have on it. Read it, "
        "then make the call again, changed if it says so; that one goes through.]\n\n{text}")
AFTER = ("[Your last {tool} call ran; it could not be held. This is what you have on "
         "such calls. Read it before you make another.]\n\n{text}")


def hold(team_dir, agent: str, tool: str, args: dict) -> str:
    """The refusal that holds this call, or "" to let it run."""
    text = check(team_dir, agent, tool, args)
    return HOLD.format(tool=tool_name(unwrap(tool, args)[0]), text=text) if text else ""


def after(team_dir, agent: str, tool: str, args: dict) -> str:
    """The note for a runtime that could not hold the call, or ""."""
    text = check(team_dir, agent, tool, args)
    return AFTER.format(tool=tool_name(unwrap(tool, args)[0]), text=text) if text else ""


def _record(team_dir, agent: str, tool: str, args: dict, items: list[dict]) -> None:
    """On the record with the call and the trigger each one came up by:
    what trigger_learn judges afterwards."""
    try:
        from . import observer
        tool, args = unwrap(tool, args)
        name = tool_name(tool)
        text = command(args) if name.lower() in SHELL else json.dumps(args, default=str)
        observer.load().record_event(
            "triggered", agent=agent, tool=name,
            shown=[i["key"].rsplit(":", 1)[0] for i in items],
            via=[i.get("via") or "" for i in items],
            call=text[:400], family=shape(tool, args, sub=False))
    except (Exception, SystemExit):         # an observer that will not load exits
        pass
