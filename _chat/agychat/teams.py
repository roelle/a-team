"""Finding teams, reading files people click on, and starting supervisors.

Everything about a team's contents goes through agyteam.record and
agyteam.actions; this module only knows where teams live on disk and how to
start the one process that drives them.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
ARTIFACT_LIMIT = 400_000          # bytes shown inline; more is truncated
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".jsonl", ".csv", ".diff", ".patch",
                 ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".js", ".ts",
                 ".html", ".css", ".rst", ".sql", ".c", ".h", ".cpp", ".go", ".rs",
                 ".java", ".kt", ".rb", ""}


class Team:
    """One team: a directory holding roster.json, bus.jsonl and the rest."""

    def __init__(self, team_dir: Path, name: str | None = None):
        self.dir = Path(team_dir).expanduser().resolve()
        self.name = name or (self.dir.parent.name if self.dir.name == "team" else self.dir.name)

    @property
    def durable(self) -> Path:
        """The team's durable root: agents/<name>/ (identity, memory) sits here."""
        return self.dir.parent if self.dir.name == "team" else self.dir

    def roster(self) -> dict:
        try:
            return json.loads((self.dir / "roster.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"agents": []}

    def roots(self) -> list[Path]:
        """Where a clicked path may be read from: the team's own tree, the
        workspaces its roster declares, and the shared directory."""
        roster = self.roster()
        declared = list(roster.get("workspaces", []))
        for a in roster.get("agents", []):
            declared += list(a.get("workspaces", []))
        out = [self.durable]
        shared = os.environ.get("AGYTEAM_SHARED_DIR")
        if shared:
            declared.append(shared)
        for p in declared:
            try:
                out.append(Path(p).expanduser().resolve())
            except OSError:
                continue
        return out

    def resolve(self, raw: str, home_too: bool = True) -> tuple[Path | None, str | None]:
        """(path, None) for something this team may show, else (None, why).

        Absolute and ~ paths are taken as they are; a relative one is looked
        for in each root (the team's tree, its workspaces, the shared
        directory). Allowed: anything under a root, and anything in your home
        directory that is not hidden -- an agent's "~/notes.md" is yours to
        read, and ~/.ssh is not something a message should open."""
        raw = (raw or "").strip()
        if not raw:
            return None, "not a path"
        roots = self.roots()
        try:
            cand = Path(raw).expanduser()
            if not cand.is_absolute():
                found = [(r / cand).resolve() for r in roots if (r / cand).exists()]
                cand = found[0] if found else (roots[0] / cand)
            p = cand.resolve()
        except (OSError, RuntimeError):
            return None, "not a path"
        if any(p == r or r in p.parents for r in roots):
            return p, None
        if not home_too:                # someone other than the operator
            return None, "outside this team and its workspaces"
        home = Path.home().resolve()
        if home in p.parents and not any(part.startswith(".") for part in p.relative_to(home).parts):
            return p, None
        return None, "outside this team's workspaces and your home directory"

    def read_artifact(self, raw: str, home_too: bool = True) -> dict:
        """A file's contents for the artifact pane, if it is somewhere this
        team may show (see resolve). Read-only."""
        p, why = self.resolve(raw, home_too)
        if p is None:
            return {"error": why, "path": raw}
        if not p.exists():
            return {"error": "no such file", "path": str(p)}
        if p.is_dir():
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))[:300]
            return {"path": str(p), "kind": "dir",
                    "entries": [{"name": e.name + ("/" if e.is_dir() else ""),
                                 "path": str(e)} for e in entries]}
        size = p.stat().st_size
        if p.suffix.lower() in IMAGE_SUFFIXES:
            return {"path": str(p), "kind": "image", "size": size}
        if p.suffix.lower() not in TEXT_SUFFIXES:
            return {"path": str(p), "kind": "binary", "size": size}
        with p.open("rb") as f:
            data = f.read(ARTIFACT_LIMIT)
        text = data.decode("utf-8", errors="replace")
        kind = ("markdown" if p.suffix.lower() == ".md" else
                "diff" if p.suffix.lower() in (".diff", ".patch") or text.startswith("diff --git")
                else "text")
        return {"path": str(p), "kind": kind, "content": text, "size": size,
                "truncated": size > ARTIFACT_LIMIT}

    def memories(self) -> dict[str, list[dict]]:
        """Each agent's saved memories, for the wiki view."""
        out = {}
        agents_dir = self.durable / "agents"
        for a in self.roster().get("agents", []):
            mem = agents_dir / a["name"] / "memory"
            items = []
            if mem.is_dir():
                for f in sorted(mem.glob("*.md")):
                    try:
                        items.append({"name": f.stem, "content": f.read_text(encoding="utf-8")[:20000]})
                    except OSError:
                        continue
            out[a["name"]] = items
        return out

    # --- the supervisor ------------------------------------------------------------

    def log_path(self) -> Path:
        return self.durable / "supervisor.log"

    def start_supervisor(self) -> dict:
        """Clear a stop and start `python -m agyteam.supervisor --daemon` for
        this team, unless one is already running. The runner and manager are
        the team's (its roster's "supervisor" block), else this app's
        environment's (AGYTEAM_RUNNER...): agy-team resolves them."""
        from agyteam import actions, heartbeat
        actions.start(self.dir)
        if heartbeat.running(self.dir):
            return {"started": False, "reason": "a supervisor is already running"}
        env = dict(os.environ, AGYTEAM_TEAM_DIR=str(self.dir))
        env.pop("AGYTEAM_AGENT", None)
        log = self.log_path().open("ab")
        proc = subprocess.Popen(
            [supervisor_python(), "-m", "agyteam.supervisor", "--daemon", "--team-dir", str(self.dir)],
            env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
        return {"started": True, "pid": proc.pid, "log": str(self.log_path())}

    def stop_supervisor(self, reason: str = "stopped from agy-chat") -> dict:
        from agyteam import actions
        actions.stop(self.dir, reason)
        return {"stopped": True, "note": "the supervisor stops at its next pass; "
                                         "turns in flight finish first"}


def supervisor_python() -> str:
    """The interpreter to run a supervisor with: AGYTEAM_PYTHON, else the
    virtualenv of the agy-team checkout this app imports (it has the runner
    dependencies this app does not need), else this interpreter."""
    explicit = os.environ.get("AGYTEAM_PYTHON")
    if explicit:
        return explicit
    try:
        import agyteam
        venv = Path(agyteam.__file__).resolve().parent.parent / ".venv" / "bin" / "python"
        if venv.exists():
            return str(venv)
    except Exception:
        pass
    return sys.executable


def discover(teams_root: Path | None, extra: list[Path]) -> dict[str, Team]:
    """{name: Team} for every team under `teams_root` (one directory per
    team, holding team/roster.json) and every directory given explicitly."""
    found: dict[str, Team] = {}
    if teams_root and Path(teams_root).expanduser().is_dir():
        for d in sorted(Path(teams_root).expanduser().iterdir()):
            if (d / "team" / "roster.json").exists():
                found[d.name] = Team(d / "team", d.name)
    for p in extra:
        t = Team(p)
        if (t.dir / "roster.json").exists():
            found[t.name] = t
    return found
