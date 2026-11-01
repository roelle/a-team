"""Update agy-chat, the agy-team it drives, and the teams; then restart.

    python -m agychat.update [--now] [--check]

The Update button runs the same thing. In order:

1. agy-team's code where the supervisors import it (`python -m
   agyteam.update --no-restart`), unless that is this app's own environment
   installed with pip, where agy-chat's pinned agy-team comes with step 2;
2. agy-chat itself: a git checkout fast-forwards, a pip install from git
   moves to the newest release tag (and with it the agy-team it pins);
3. every team this app shows, restarted gracefully on the new code
   (`python -m agyteam.update --no-code`): no new turns, the ones in flight
   finish, then each supervisor restarts itself;
4. this server, in place, by whoever runs it (App.reexec for the button).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .teams import supervisor_python


def _agyteam(args: list[str], log) -> dict:
    """Run `python -m agyteam.update --json ...` under the supervisors'
    interpreter, passing its progress lines to `log`."""
    proc = subprocess.Popen([supervisor_python(), "-m", "agyteam.update", "--json", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stderr:
        if line.strip():
            log(line.rstrip())
    out = proc.stdout.read()
    proc.wait()
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"agyteam.update exited {proc.returncode} with no result"}


def checkout() -> Path | None:
    repo = Path(__file__).resolve().parent.parent
    return repo if (repo / ".git").exists() else None


def agyteam_shares_this_env() -> bool:
    """True when the supervisors run on this interpreter and agyteam is a
    pip install here: agy-chat's own upgrade brings its pinned agy-team."""
    from agyteam import update as up
    return Path(supervisor_python()).resolve() == Path(sys.executable).resolve() \
        and up.checkout() is None


def plan_self() -> dict:
    from agyteam import update as up
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
        raw = metadata.distribution("agy-chat").read_text("direct_url.json")
        url = (json.loads(raw) if raw else {}).get("url", "")
    except Exception:
        return None
    return url[4:] if url.startswith("git+") else (url or None)


def update_self(p: dict) -> str:
    from agyteam import update as up
    if p["kind"] in ("git", "git-tag"):
        return up.update_code(p)
    if p["kind"] == "pip":
        if not p.get("target"):
            raise up.UpdateError(f"no release tags at {p['url']}")
        up._run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
                 f"agy-chat @ git+{p['url']}@{p['target']}"])
        return f"installed agy-chat {p['target']}"
    raise up.UpdateError("agy-chat is installed neither from a git checkout nor from "
                         "git with pip; update it the way you installed it")


def run(team_dirs: list[Path], now: bool = False, log=print) -> dict:
    """Steps 1-3. Returns what each did; raises nothing (errors are reported)."""
    from agyteam import update as up
    result: dict = {}
    if not agyteam_shares_this_env():
        log("updating agy-team…")
        result["agyteam"] = _agyteam(["--no-restart"], log)
        if result["agyteam"].get("error"):
            return {**result, "error": f"agy-team: {result['agyteam']['error']}"}
        for k in ("code", "plugin"):
            if result["agyteam"].get(k):
                log(result["agyteam"][k])
    log("updating agy-chat…")
    try:
        result["agychat"] = update_self(plan_self())
        log(result["agychat"])
    except up.UpdateError as e:
        return {**result, "error": f"agy-chat: {e}"}
    log("restarting the teams gracefully…")
    args = ["--no-code", "--no-wait"] + (["--now"] if now else [])
    for td in team_dirs:
        args += ["--team-dir", str(td)]
    result["teams"] = _agyteam(args, log).get("teams", [])
    for t in result["teams"]:
        if t.get("state") != "not running":
            log(f"{t['team']}: {t['state']}"
                + (f" (waiting for {', '.join(t['waiting_for'])})" if t.get("waiting_for") else ""))
    return result


def main(argv=None) -> int:
    import argparse
    from .teams import discover
    ap = argparse.ArgumentParser(prog="python -m agychat.update", description=__doc__.split("\n\n")[0])
    ap.add_argument("--now", action="store_true", help="cancel turns in flight instead of waiting")
    ap.add_argument("--check", action="store_true", help="say what would change; change nothing")
    ap.add_argument("--teams-root", default="~/agy-teams")
    ap.add_argument("--team-dir", action="append", default=[])
    a = ap.parse_args(argv)
    if a.check:
        print(json.dumps({"agychat": plan_self(),
                          "agyteam": _agyteam(["--check"], print).get("plan")}, indent=2))
        return 0
    teams = discover(Path(a.teams_root).expanduser(), [Path(p) for p in a.team_dir])
    out = run([t.dir for t in teams.values()], now=a.now)
    if out.get("error"):
        print(f"error: {out['error']}", file=sys.stderr)
        return 1
    print("done; restart agy-chat itself to load its new code (the Update button does this)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
