"""One answer to "may this agent make this tool call?", asked from three places.

Role policy used to be enforced in exactly one of them: the SDK runner, which
withholds a tool's schema for `tools_off`. Everywhere else the roster entry
was a description. On a host runtime the manager had every native tool,
assigned tasks to implementers directly, and could message anyone; an
integrator wrote six hundred lines of host hook to get back what the roster
already said. So the rule lives here, once, and is asked:

- by the SDK session builder, as a pre-tool-call hook;
- by `python -m ateam.hook_pre_tool_use`, a generic hook for any host that
  can run a command before a tool call and read its exit code;
- inside the bus and task servers themselves, so what those servers do --
  who an agent may message, who may hand work to whom -- holds on a host
  with no hooks at all.

## What the roster can say

Per agent:

    "tools_off":        ["run_command", ...]   tool names this agent may not call
    "allowed_send_to":  ["manager"]            who it may message ("user" included
                                               only if listed); absent = anyone
    "assigns_tasks":    false                  may not create tasks for others or
                                               hand its own tasks to others
    "workspaces":       [...]                  with the team's, the only paths its
                                               file tools may touch; see below
    "confined":         false                  opt out of the path rule (a role
                                               that maintains the platform itself)

    "max_inbound_bytes": 2800                  the largest message teammates may send
                                               it; for a role that reads only its
                                               inbox (no file tools), where a long
                                               message would arrive cut short
    "allowed_mcp_servers": ["ateam_bus"]     on a host whose generic MCP call
                                               names the server in its arguments,
                                               the only servers it may call
    "refuse_paths":     ["/srv/checkout"]      paths its file tools may never touch,
                                               whatever the workspaces say, and its
                                               shell commands may not name
    "write_roots":      ["/srv/work"]          the only places it may write; reads
                                               are not limited. See below
    "allowed_task_projects": ["home", "web-*"] the task projects it may create,
                                               claim, change or list tasks in;
                                               absent = every project
    "task_projects_limit": "create_assign"     allowed_task_projects holds only
                                               for creating and assigning tasks;
                                               it sees and works any task.
                                               Also team-wide under "policy"

Team-wide, under "policy" (each also accepted per agent):

    "detach_commands":  ["make bench", "run_sim.*"]   regular expressions; a
                        shell command matching one is refused unless it is
                        already detached (nohup, setsid, or a trailing &). On
                        a hosted UI a foreground multi-hour command hangs the
                        turn to its timeout; sixteen such timeouts in one
                        episode.
    "forbidden_commands": [{"pattern": "launch_sim", "message": "the operator
                        starts sims"}]   refused outright, with that message
                        and no suggestion to detach. A plain string is a
                        pattern with a default message.
    "refuse_paths":     [...]                  as above, for everyone
    "write_roots":      [...]                  as above, for everyone; an agent's
                                               own are added to the team's
    "refuse_partial_images": false             let file tools read an image that
                                               is still being written (refused
                                               by default; see below)

## Paths

A native file tool on a path outside the agent's workspaces raises an
interactive permission dialog on a hosted UI, which nobody can click in a
background conversation, and the turn hangs until its timeout. The path rule
exists for that, and only applies where a workspace is declared for the agent
or the team: an agent with none declared is not confined by this file.
Allowed roots are the declared workspaces, the agent's own durable workspace,
and the team's shared directory. The team directory is never allowed -- it is
the record, and agents reach it through the servers.

`refuse_paths` is the other form of the same rule, for teams whose agents
legitimately roam: nothing is confined, but a named path -- a source checkout
the UI treats as out-of-workspace, a directory with answer keys -- is refused
wherever it is reached from. Both forms are checked on the same arguments.
A shell command that names a refused path is refused too, after expanding
`~` and `$HOME` and removing quotes. That is a text match on the command, so
it stops `cat ~/answers/key.json` and not a path the command builds at run
time; treat it as a guard against reaching for the path, not a sandbox.

`write_roots` limits writes and leaves reads alone, for a team whose agents
read widely but must change only their own tree. Once any are declared,
every path a write names -- a file tool that writes, or a shell command's
redirections, destinations and in-place edits (see `_written_by`) -- must be
under one of them, the agent's own durable workspace or the shared
directory. /dev/null and the standard streams are always writable.

A shell command is checked twice against `refuse_paths`: as text, as above,
and as words. The words are the command split the way the shell would --
quotes kept together, `--out=PATH` and `of=PATH` opened up, `sh -c '...'`
split again -- with each one that is a path resolved against the call's
working directory. That catches `cat key.json` run from inside a refused
directory, which the text never names.

## Images still being written

An image a file tool reads goes to the model with the next request. One
read while a render is still writing it is a truncated file, which the
provider rejects -- and on some runtimes every later request in that
conversation is rejected with it. So a .png, .jpg, .gif or .webp without
its end marker (PNG's IEND, JPEG's EOI, GIF's trailer, a RIFF size the file
has reached) is refused, with a note to wait and read it again. A JPEG last
changed more than ten minutes ago is let through without one: nothing is
writing it, and data after the end marker is legal there (a phone's motion
photo appends a video). `"policy": {"refuse_partial_images": false}` turns
it off.

The rule is applied to any tool whose name looks like a file operation, on
any string argument that resolves to an absolute path. That is a heuristic,
and a deliberate one: hosts name their file tools differently, and a false
refusal costs one tool call with a clear message, where a false allow costs a
hung turn.
"""
import fnmatch
import re
import time
from pathlib import Path

from . import roster as roster_lib

FILE_TOOL = re.compile(r"file|dir|path|read|write|edit|view|glob|grep|list|"
                       r"search|replace|create|delete|remove|move|rename|append|"
                       r"open|save|cat|ls|find", re.I)
SHELL_TOOL = re.compile(r"command|shell|bash|exec|terminal|run", re.I)
DETACHED = re.compile(r"\bnohup\b|\bsetsid\b|&\s*$|\bdisown\b")
#: File tools that change what is on disk, as opposed to reading it.
WRITE_TOOL = re.compile(r"write|edit|create|delete|remove|move|rename|append|"
                        r"replace|save|patch|notebook|mkdir|touch", re.I)

#: Our own servers' tools: policy is applied to their *arguments* inside the
#: server, never to their names as file operations.
TEAM_TOOLS = {"send_to_teammate", "broadcast", "check_inbox", "list_teammates",
              "post_to_channel", "list_channels",
              "record_review", "list_reviews", "record_retro", "list_retros",
              "create_task", "claim_task", "update_task", "complete_task",
              "list_tasks", "save_memory", "read_memory", "delete_memory", "merge_memories",
              "memory_index", "review_memory", "whoami", "my_capabilities",
              "my_activity"}


def _entry(roster: dict, agent: str) -> dict:
    for a in roster.get("agents", []):
        if a.get("name") == agent:
            return a
    return {}


def load_roster(team_dir: Path | str | None) -> dict:
    if team_dir is None:
        return {"agents": []}
    try:
        return roster_lib.load(Path(team_dir) / "roster.json")
    except Exception:
        return {"agents": []}


def allowed_roots(agent: str, roster: dict, team_dir: Path | str | None) -> list[Path] | None:
    """Where this agent's file tools may go, or None if it is not confined."""
    entry = _entry(roster, agent)
    if entry.get("confined") is False:
        return None
    declared = list(roster.get("workspaces", [])) + list(entry.get("workspaces", []))
    if not declared:
        return None
    roots = [Path(p).expanduser().resolve() for p in declared]
    try:
        from . import scope
        scopes = scope.load()
        roots.append(scopes.agent_workspace(agent).resolve())
        roots.append(scopes.shared_dir().resolve())
    except Exception:
        pass
    return roots


def _patterns(roster: dict, entry: dict, key: str) -> list:
    team = (roster.get("policy") or {}).get(key) or []
    mine = entry.get(key) or (entry.get("policy") or {}).get(key) or []
    return list(team) + list(mine)


def _matches(pattern: str, text: str) -> bool:
    """A regular expression, or a plain substring if it is not a valid one."""
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return pattern in text


def refused_paths(agent: str, roster: dict) -> list[Path]:
    entry = _entry(roster, agent)
    out = []
    for raw in _patterns(roster, entry, "refuse_paths"):
        try:
            out.append(Path(raw).expanduser().resolve())
        except OSError:
            continue
    return out


def _raw_refused(roster: dict, entry: dict) -> list[Path]:
    """refuse_paths as written, ~ expanded but symlinks not followed: a
    command names a path the way a person wrote it, not as it resolves."""
    out = []
    for raw in _patterns(roster, entry, "refuse_paths"):
        try:
            out.append(Path(raw).expanduser())
        except (OSError, RuntimeError):
            continue
    return out


_HOME_VAR = re.compile(r"\$\{HOME\}|\$HOME\b")
_TILDE = re.compile(r"(?<![\w~/.-])~(?=/|$|\s)")
_PATH_CHAR = r"[\w.~/-]"


def _refused_path_in_command(cmd: str, refused: list[Path]) -> str | None:
    """The refused path a shell command names, if any.

    Best effort, and documented as such: it catches the path written out --
    with ~ or $HOME, or split by quotes ('/srv/'"checkout") -- and nothing
    computed at run time (a cd and a relative path, a variable, a glob).
    It stops an agent that reaches for the path, not one that sets out to
    hide it; the file-tool rule and a container are the boundary.
    """
    if not cmd or not refused:
        return None
    home = str(Path.home())
    text = cmd.replace("'", "").replace('"', "")
    text = _HOME_VAR.sub(home, text)
    text = _TILDE.sub(home, text)
    for d in dict.fromkeys(str(p).rstrip("/") for p in refused if str(p) not in ("", "/")):
        if re.search(rf"(?<!{_PATH_CHAR}){re.escape(d)}(?=/|$|[^\w.~-])", text):
            return d
    return None


def _paths_in(args: dict) -> list[Path]:
    cwd = str((args or {}).get("Cwd") or (args or {}).get("cwd") or "")
    out = []
    for v in (args or {}).values():
        if not isinstance(v, str) or not v or len(v) > 4096 or "\n" in v:
            continue
        p = Path(v).expanduser()
        if not p.is_absolute():
            if not cwd:
                continue
            p = Path(cwd) / p
        try:
            out.append(p.resolve())
        except OSError:
            continue
    return out


def other_workspaces(agent: str, roster: dict) -> dict[Path, str]:
    """{workspace: owner} for every teammate's private durable workspace."""
    try:
        from . import scope
        root = scope.load().durable_root() / "agents"
    except Exception:
        return {}
    out = {}
    for a in roster.get("agents", []):
        name = a.get("name")
        if name and name != agent:
            out[(root / name).resolve()] = name
    return out


def _owner(p: Path, others: dict[Path, str]) -> str | None:
    return next((who for ws, who in others.items() if p == ws or ws in p.parents), None)


_SEGMENT = re.compile(r"\|\||&&|[;|\n]")
_REDIRECT = re.compile(r"(?:^|[^<>&0-9])(?:[0-9]|&)?>>?\|?\s*(\S+)")
_COPYLIKE = {"cp", "mv", "rsync", "install", "ln", "scp"}
_TOUCHES = {"rm", "rmdir", "unlink", "touch", "mkdir", "chmod", "chown", "truncate",
            "shred", "tee"}


def _written_by(cmd: str, cwd: str = "") -> list[Path]:
    """Paths a shell command looks like it writes. Best effort, by design.

    Redirections, the destination of cp/mv/rsync/install/ln (the last
    argument, or -t DIR), every argument of rm/touch/mkdir/chmod/tee and
    their kind, sed -i's files, dd's of=, and the same again inside
    `sh -c` / `bash -c`. A source being read -- `cp theirs/x mine/`,
    `pytest theirs/test_x.py` -- is not a write. What it cannot see is a
    path computed at run time, or a write made by a program it runs.
    """
    import shlex
    home = str(Path.home())

    def norm(word: str) -> Path | None:
        w = word.strip("'\"")
        if not w or w.startswith(("-", "&")):          # 2>&1 names no file
            return None
        w = _HOME_VAR.sub(home, w)
        if w == "~" or w.startswith("~/"):
            w = home + w[1:]
        p = Path(w)
        if not p.is_absolute():
            if not cwd:
                return None
            p = Path(cwd) / p
        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return None

    out: list[Path] = []
    for seg in _SEGMENT.split(cmd or ""):
        for m in _REDIRECT.finditer(seg):
            if (p := norm(m.group(1))) is not None:
                out.append(p)
        bare = _REDIRECT.sub(" ", seg)
        try:
            words = shlex.split(bare)
        except ValueError:
            words = bare.split()
        while words and ("=" in words[0] and not words[0].startswith("-")
                         or words[0] in ("sudo", "env", "nohup", "setsid", "time", "exec")):
            words = words[1:]
        if not words:
            continue
        prog, args = Path(words[0]).name, words[1:]
        if prog in ("sh", "bash", "zsh") and "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args):
                out += _written_by(args[i + 1], cwd)
            continue
        plain = [a for a in args if not a.startswith("-")]
        if prog in _COPYLIKE:
            if "-t" in args and args.index("-t") + 1 < len(args):
                targets = [args[args.index("-t") + 1]]
            else:
                tgt = next((a.split("=", 1)[1] for a in args
                            if a.startswith("--target-directory=")), None)
                targets = [tgt] if tgt else plain[-1:] if len(plain) >= 2 else []
        elif prog in _TOUCHES:
            targets = plain
        elif prog == "sed" and any(a == "-i" or a.startswith("-i") or a == "--in-place"
                                   for a in args):
            targets = plain[1:] if not any(a in ("-e", "-f") for a in args) else plain
        elif prog == "dd":
            targets = [a[3:] for a in args if a.startswith("of=")]
        else:
            targets = []
        out += [p for p in map(norm, targets) if p is not None]
    return out


def write_roots(agent: str, roster: dict) -> list[Path] | None:
    """Where this agent may write, or None if writes are not limited."""
    entry = _entry(roster, agent)
    declared = _patterns(roster, entry, "write_roots")
    if not declared:
        return None
    roots = []
    for raw in declared:
        try:
            roots.append(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    try:
        from . import scope
        scopes = scope.load()
        roots.append(scopes.agent_workspace(agent).resolve())
        roots.append(scopes.shared_dir().resolve())
    except Exception:
        pass
    return roots


#: Written to by every other command (2>/dev/null, >&2): never a write to
#: refuse. /dev/stdout resolves through /proc.
_STREAMS = (Path("/dev"), Path("/proc"))


def _within(p: Path, roots) -> bool:
    return any(p == r or r in p.parents for r in roots)


_REDIRECT_PREFIX = re.compile(r"^\d*[<>]+&?")
_PATHISH = re.compile(r"^(?:/|~|\.\.?/|\$HOME\b|\$\{HOME\})|/")


def _shell_paths(cmd: str, cwd: str = "") -> list[Path]:
    """The paths a shell command names, word by word: split as the shell
    would (quotes keep a word together), `--out=PATH` and `of=PATH` opened
    up, `sh -c '...'` split again, each resolved against `cwd`. Best effort,
    like everything here: a path built at run time is not seen."""
    import os.path
    import shlex
    home = str(Path.home())
    out: list[Path] = []
    for seg in _SEGMENT.split(cmd or ""):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        if words and Path(words[0]).name in ("sh", "bash", "zsh") and "-c" in words:
            i = words.index("-c")
            if i + 1 < len(words):
                out += _shell_paths(words[i + 1], cwd)
        for w in words:
            for cand in (w, w.split("=", 1)[1] if "=" in w else ""):
                cand = _REDIRECT_PREFIX.sub("", cand)
                if not cand or not _PATHISH.search(cand):
                    continue
                cand = _HOME_VAR.sub(home, cand)
                if cand == "~" or cand.startswith("~/"):
                    cand = home + cand[1:]
                path = Path(cand)
                if not path.is_absolute():
                    if not cwd:
                        continue
                    path = Path(cwd) / path
                out.append(Path(os.path.normpath(path)))
    return out


def _refused_word(cmd: str, cwd: str, refused: list[Path]) -> str | None:
    """The refused path a command runs in or names as a word, if any."""
    import os.path
    if not refused:
        return None
    here = [Path(os.path.normpath(Path(cwd).expanduser()))] if cwd else []
    for p in here + _shell_paths(cmd, cwd):
        try:
            forms = {p, p.resolve()}
        except (OSError, RuntimeError):
            forms = {p}
        for d in refused:
            if str(d) in ("", "/"):
                continue
            if any(f == d or d in f.parents for f in forms):
                return str(d)
    return None


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
#: A JPEG unchanged this long is not being written, end marker or not.
SETTLED_S = 600


def image_still_writing(p: Path) -> bool:
    """True if `p` is an image a writer has not finished: no end marker yet.
    False for anything else, including a file that is not an image at all."""
    try:
        if p.suffix.lower() not in IMAGE_SUFFIXES or not p.is_file():
            return False
        st = p.stat()
        with p.open("rb") as f:
            head = f.read(16)
            f.seek(max(0, st.st_size - 64))
            tail = f.read()
    except OSError:
        return False
    if len(head) < 16:
        return True                      # not even a header yet
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return b"IEND" not in tail
    if head.startswith(b"\xff\xd8"):
        if b"\xff\xd9" in tail.rstrip(b"\x00\r\n\t "):
            return False
        return time.time() - st.st_mtime < SETTLED_S
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return not tail.rstrip(b"\x00").endswith(b";")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return st.st_size < int.from_bytes(head[4:8], "little") + 8
    return False


def allowed_task_projects(agent: str, roster: dict) -> list[str] | None:
    """The task projects this agent may work in (patterns), or None for all."""
    v = _entry(roster, agent).get("allowed_task_projects")
    if v is None:
        return None
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def task_projects_limit_all(agent: str, roster: dict) -> bool:
    """Does allowed_task_projects hold for everything the agent does with
    tasks (the default), or only for creating and assigning them?

    Kept to its projects for listing too, an agent that coordinates across
    them could not see the tasks it was meant to follow, or the one a
    teammate asked it to pick up. "task_projects_limit": "create_assign" (in
    its entry, or team-wide under "policy") limits where it makes and hands
    out work, and leaves it free to read, claim and finish any task.
    """
    entry = _entry(roster, agent)
    v = entry["task_projects_limit"] if "task_projects_limit" in entry else \
        (roster.get("policy") or {}).get("task_projects_limit")
    return str(v or "all").strip().lower().replace("-", "_") not in ("create_assign", "assign")


def task_project_refusal(agent: str, roster: dict, project: str | None,
                         what: str = "that task") -> str | None:
    """A refusal if `project` is outside the agent's task projects."""
    allowed = allowed_task_projects(agent, roster)
    if allowed is None or any(fnmatch.fnmatchcase(project or "", pat) for pat in allowed):
        return None
    where = f"project {project!r}" if project else "no project"
    return (f"[refused: {what} is in {where}, and {agent} works only in task "
            f"projects {', '.join(allowed) or '(none)'} (roster "
            f"allowed_task_projects). Ask a teammate who works there]")


def check_tool_policy(agent: str, tool: str, args: dict | None,
                      team_dir: Path | str | None = None,
                      roster: dict | None = None,
                      check_paths: bool = True) -> str | None:
    """A refusal the agent can act on, or None if the call is allowed.

    `check_paths=False` skips the workspace rule, for a runner that confines
    agents itself (the SDK) and whose idea of the allowed roots is wider.
    """
    roster = roster if roster is not None else load_roster(team_dir)
    entry = _entry(roster, agent)
    args = args or {}
    name = str(tool or "")

    # A host with one generic MCP-call tool names the real tool in its
    # arguments; tools_off has to see through it or it holds only for the
    # tools that host happens to expose natively.
    inner = next((str(args[k]) for k in ("ToolName", "tool_name", "toolName")
                  if isinstance(args.get(k), str) and args.get(k)), None)
    for called in (name, inner):
        if called and called in (entry.get("tools_off") or []):
            return (f"[refused: {called} is not available to {agent} on this team "
                    f"(roster tools_off). Delegate it to a teammate who has it]")

    if name in ("send_to_teammate", "broadcast"):
        allowed = entry.get("allowed_send_to")
        if allowed is not None:
            to = args.get("to") if name == "send_to_teammate" else None
            if name == "broadcast":
                return (f"[refused: {agent} may only message "
                        f"{', '.join(allowed) or 'nobody'} (roster allowed_send_to); "
                        f"broadcast reaches everyone]")
            if to and to not in allowed:
                return (f"[refused: {agent} may only message "
                        f"{', '.join(allowed) or 'nobody'} (roster allowed_send_to), "
                        f"not '{to}'. Route it through them]")

    if name == "send_to_teammate":
        to = str(args.get("to") or "")
        cap = _entry(roster, to).get("max_inbound_bytes")
        size = len(str(args.get("content") or "").encode("utf-8"))
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 and size > cap:
            return (f"[refused: {to} takes messages of at most {cap} bytes (roster "
                    f"max_inbound_bytes) and this one is {size}. Write the detail "
                    f"to a file in the shared directory and send {to} a short "
                    f"summary with its path]")

    if name in ("create_task", "update_task", "claim_task") and entry.get("assigns_tasks") is False:
        owner = args.get("owner")
        if name == "create_task" and owner and owner != agent:
            return (f"[refused: {agent} does not assign tasks on this team "
                    f"(roster assigns_tasks: false). Create it for yourself, or "
                    f"ask the coordinator to assign it]")
        if name == "update_task" and owner and owner != agent:
            return (f"[refused: {agent} does not hand tasks to others on this "
                    f"team (roster assigns_tasks: false)]")

    allowed_servers = entry.get("allowed_mcp_servers")
    if allowed_servers:                 # a roster from before the rename
        allowed_servers = [s.replace("agyteam_", "ateam_", 1) if isinstance(s, str) else s
                           for s in allowed_servers]
    if allowed_servers is not None and name not in TEAM_TOOLS:
        # A host with one generic MCP-call tool names the server in the
        # arguments; that is the only place "which server" is visible.
        server = next((str(args[k]) for k in
                       ("ServerName", "server_name", "serverName", "server",
                        "mcp_server", "mcpServer") if args.get(k)), None)
        if server and server not in allowed_servers:
            return (f"[refused: {agent} may call only these MCP servers: "
                    f"{', '.join(allowed_servers) or 'none'} (roster "
                    f"allowed_mcp_servers), not '{server}']")

    written: list[Path] = []
    if name not in TEAM_TOOLS:
        if SHELL_TOOL.search(name):
            cmd = str(args.get("command") or args.get("cmd") or args.get("CommandLine") or "")
            cwd = str(args.get("Cwd") or args.get("cwd") or "")
            written = _written_by(cmd, cwd) if cmd else []
        elif WRITE_TOOL.search(name) and FILE_TOOL.search(name):
            written = _paths_in(args)

    # The team directory is the record, and roster.json in it is every
    # permission on the team. No agent writes there, confined or not: the
    # servers do it for them. This was enforced only for an agent with a
    # declared workspace, so one given no permissions at all was the one
    # agent that could rewrite them -- and downstream, one did, then used
    # the access it had granted itself. As everywhere in this file, a write
    # a program makes at run time is not visible here; the supervisor
    # reverts a roster changed during a turn.
    if written and team_dir:
        td = Path(team_dir).expanduser().resolve()
        hit = next((p for p in written if p == td or td in p.parents), None)
        if hit:
            return (f"[refused: {hit} is in the team directory, the record "
                    f"ateam keeps of this team; no agent writes there, and "
                    f"its roster is the operator's. Use the team tools. If "
                    f"you need a permission you do not have, ask for it -- "
                    f"do not work around this]")

    # Each agent's durable workspace -- its identity, its memory, whatever
    # it keeps there -- is its own. Read freely; written only by its owner.
    # This holds for every agent, confined or not: one implementer with a
    # shell copied into a reviewer's directory and overwrote the evidence
    # the review rested on. `"policy": {"private_workspaces": false}` turns
    # it off for a team that shares them on purpose.
    private = (roster.get("policy") or {}).get("private_workspaces", True) is not False
    if private and written:
        others = other_workspaces(agent, roster)
        for p in written:
            who = _owner(p, others)
            if who:
                return (f"[refused: {p} is in {who}'s workspace, which only "
                        f"{who} writes. Put what you are handing over in the "
                        f"shared directory and send {who} the path]")

    roots = write_roots(agent, roster) if written else None
    if roots is not None:
        for p in written:
            if not _within(p, _STREAMS) and not _within(p, roots):
                return (f"[refused: {p} is outside where {agent} may write "
                        f"({', '.join(str(r) for r in roots[:3])}"
                        f"{', ...' if len(roots) > 3 else ''}; roster write_roots). "
                        f"Reading it is fine. Write under one of those, or ask "
                        f"the operator to add the path to write_roots]")

    if SHELL_TOOL.search(name) and name not in TEAM_TOOLS:
        cmd = str(args.get("command") or args.get("cmd") or args.get("CommandLine") or "")
        for rule in _patterns(roster, entry, "forbidden_commands"):
            pattern = rule.get("pattern", "") if isinstance(rule, dict) else str(rule)
            if pattern and _matches(pattern, cmd):
                why = (rule.get("message") if isinstance(rule, dict) else None) or \
                    "this team does not let agents run it"
                return f"[refused: commands matching '{pattern}' are not for you to run: {why}]"
        refused = refused_paths(agent, roster) + _raw_refused(roster, entry)
        named = (_refused_path_in_command(cmd, refused)
                 or _refused_word(cmd, str(args.get("Cwd") or args.get("cwd") or ""), refused))
        if named:
            return (f"[refused: this command names {named}, which this team's "
                    f"agents may not touch (policy refuse_paths). Reach it "
                    f"another way or leave it alone]")
        hit = next((p for p in _patterns(roster, entry, "detach_commands")
                    if p and _matches(p, cmd)), None)
        if hit and not DETACHED.search(cmd):
            return (f"[refused: commands matching '{hit}' run for hours and a "
                    f"foreground run hangs this turn until it times out. Run it "
                    f"detached (nohup ... > log 2>&1 &), then track it with a task "
                    f"reminder (update_task with check_after, or until=...)]")

    if (name not in TEAM_TOOLS and FILE_TOOL.search(name) and not WRITE_TOOL.search(name)
            and not SHELL_TOOL.search(name)
            and (roster.get("policy") or {}).get("refuse_partial_images", True) is not False):
        for p in _paths_in(args):
            if image_still_writing(p):
                return (f"[refused: {p} is an image that is still being written "
                        f"(it has no end marker yet). Read now, it would reach "
                        f"the model truncated, and the request would be "
                        f"rejected. Wait for whatever writes it to finish, then "
                        f"read it again]")

    if check_paths and name not in TEAM_TOOLS and FILE_TOOL.search(name):
        paths = _paths_in(args)
        denied = refused_paths(agent, roster)
        for p in paths:
            hit = next((d for d in denied if p == d or d in p.parents), None)
            if hit:
                return (f"[refused: {p} is under {hit}, which this team's file "
                        f"tools may not touch (policy refuse_paths). Reach it "
                        f"another way or leave it alone]")
        roots = allowed_roots(agent, roster, team_dir)
        if roots is not None:
            td = Path(team_dir).expanduser().resolve() if team_dir else None
            for p in paths:
                inside = any(p == r or r in p.parents for r in roots)
                in_record = td is not None and (p == td or td in p.parents)
                if in_record or not inside:
                    return (f"[refused: {p} is outside {agent}'s workspaces "
                            f"({', '.join(str(r) for r in roots[:3])}"
                            f"{', ...' if len(roots) > 3 else ''}). On this host a "
                            f"file tool outside your workspace hangs the turn. Use "
                            f"the shell, or stage the file under a workspace]")
    return None
