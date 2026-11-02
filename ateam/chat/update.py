"""Update a-team and the teams; then restart.

    python -m ateam.chat.update [--now] [--check]

The Update button runs the same thing. In order:

1. the code: a git checkout fast-forwards, a pip install from git moves to the
   newest release tag;
2. every team this app shows, restarted gracefully on the new code
   (`python -m ateam.update --no-code`): no new turns, the ones in flight
   finish, then each supervisor restarts itself;
3. this server, in place, by whoever runs it (App.reexec for the button).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .teams import supervisor_python


def _ateam(args: list[str], log) -> dict:
    """Run `python -m ateam.update --json ...` under the supervisors'
    interpreter, passing its progress lines to `log`."""
    proc = subprocess.Popen([supervisor_python(), "-m", "ateam.update", "--json", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stderr:
        if line.strip():
            log(line.rstrip())
    out = proc.stdout.read()
    proc.wait()
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"ateam.update exited {proc.returncode} with no result"}


def checkout() -> Path | None:
    repo = Path(__file__).resolve().parent.parent
    return repo if (repo / ".git").exists() else None


def plan_self() -> dict:
    from ateam import update as up
    repo = checkout()
    if repo is not None:
        return up.plan(repo=repo)
    url = _pip_source()
    if url:
        tags = [line.rsplit("refs/tags/", 1)[-1] for line in
                up._run(["git", "ls-remote", "--tags", "--refs", url]).splitlines()]
        return {"kind": "pip", "url": url, "target": up._newest_tag(tags)}
    return {"kind": "unknown"}


def _pip_source() -> str | None:
    try:
        from importlib import metadata
        raw = metadata.distribution("a-team").read_text("direct_url.json")
        url = (json.loads(raw) if raw else {}).get("url", "")
    except Exception:
        return None
    return url[4:] if url.startswith("git+") else (url or None)


def update_self(p: dict) -> str:
    from ateam import update as up
    if p["kind"] in ("git", "git-tag"):
        return up.update_code(p)
    if p["kind"] == "pip":
        if not p.get("target"):
            raise up.UpdateError(f"no release tags at {p['url']}")
        up._run([*up.installer(), f"a-team @ git+{p['url']}@{p['target']}"])
        return f"installed a-team {p['target']}"
    raise up.UpdateError("a-team is installed neither from a git checkout nor from "
                         "git with pip; update it the way you installed it")


def run(team_dirs: list[Path], now: bool = False, log=print) -> dict:
    """Steps 1-3. Returns what each did; raises nothing (errors are reported)."""
    from ateam import update as up
    result: dict = {}
    log("updating a-team…")
    try:
        result["code"] = update_self(plan_self())
        log(result["code"])
    except up.UpdateError as e:
        return {**result, "error": f"a-team: {e}"}
    log("restarting the teams gracefully…")
    args = ["--no-code", "--no-wait"] + (["--now"] if now else [])
    for td in team_dirs:
        args += ["--team-dir", str(td)]
    result["teams"] = _ateam(args, log).get("teams", [])
    for t in result["teams"]:
        if t.get("state") != "not running":
            log(f"{t['team']}: {t['state']}"
                + (f" (waiting for {', '.join(t['waiting_for'])})" if t.get("waiting_for") else ""))
    return result


def main(argv=None) -> int:
    import argparse
    from .teams import discover
    ap = argparse.ArgumentParser(prog="python -m ateam.chat.update", description=__doc__.split("\n\n")[0])
    ap.add_argument("--now", action="store_true", help="cancel turns in flight instead of waiting")
    ap.add_argument("--check", action="store_true", help="say what would change; change nothing")
    ap.add_argument("--teams-root", default=scope.default_teams_root())
    ap.add_argument("--team-dir", action="append", default=[])
    a = ap.parse_args(argv)
    if a.check:
        print(json.dumps(plan_self(), indent=2))
        return 0
    teams = discover(Path(a.teams_root).expanduser(), [Path(p) for p in a.team_dir])
    out = run([t.dir for t in teams.values()], now=a.now)
    if out.get("error"):
        print(f"error: {out['error']}", file=sys.stderr)
        return 1
    print("done; restart a-team itself to load its new code (the Update button does this)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
