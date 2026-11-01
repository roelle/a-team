"""Starting a team from nothing, without a checkout or a shell.

    python -m ateam.starter new <name> [--runner <spec>]
    python -m ateam.starter runtimes

`runtimes()` says which runtimes this machine can use now, and what each still
needs. It looks for a key but never reads one out, and it takes none: a key
belongs in the environment the supervisor starts in.

`create_team()` writes the starter roster and records the runtime in the
roster's "supervisor" block, so the team runs the same whoever starts it. The
roster is the one the plugin installer seeds, plugin/starter_roster.json.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import scope

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
AGY_SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


class StarterError(Exception):
    """Something a person can fix; the message says what."""


def plugin_dir() -> Path:
    """plugin/ in a checkout, ateam/plugin/ in an installed package."""
    here = Path(__file__).resolve().parent
    for d in (here / "plugin", here.parent / "plugin"):
        if (d / "starter_roster.json").exists():
            return d
    raise StarterError(f"no plugin directory beside {here}: reinstall ateam")


def starter_roster(team: str) -> dict:
    text = (plugin_dir() / "starter_roster.json").read_text(encoding="utf-8")
    return json.loads(text.replace("$TEAM", team))


def _agy_auto_approves() -> bool:
    try:
        return json.loads(AGY_SETTINGS.read_text(encoding="utf-8")).get(
            "toolPermission") == "always-proceed"
    except (OSError, ValueError, AttributeError):
        return False


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:         # its parent package is missing too
        return False


def runtimes() -> list[dict]:
    """[{"id", "label", "runner", "ready", "needs", "note"}], best first.

    `needs` is what stands between this machine and that runtime, or "".
    """
    def row(id_, label, runner, needs, note=""):
        return {"id": id_, "label": label, "runner": runner, "ready": not needs,
                "needs": needs, "note": note}

    agy = shutil.which("agy")
    agy_note = "" if not agy or _agy_auto_approves() else (
        'Agents will stop at any tool that needs approval until you set '
        f'"toolPermission": "always-proceed" in {AGY_SETTINGS}.')
    gemini = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    claude_key = any(os.environ.get(k) for k in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"))

    def needs(package: str, extra: str, module: str, key: bool, var: str) -> str:
        missing = []
        if not _has(module):
            missing.append(f'the {package} package (pip install "a-team[{extra}]", '
                           f"or pipx inject)")
        if not key:
            missing.append(f"{var} in the environment you start this from")
        return " and ".join(missing)

    return [row("agy", "Antigravity CLI", "ateam.runner_agy:AgyRunner",
                "" if agy else "the agy command on your PATH", agy_note),
            row("sdk", "Antigravity SDK, with a Gemini API key", "ateam.runner_sdk:SdkRunner",
                needs("google-antigravity", "sdk", "google.antigravity", bool(gemini),
                      "GEMINI_API_KEY")),
            row("claude", "Claude, with an Anthropic API key",
                "ateam.runner_claude:ClaudeRunner",
                needs("claude-agent-sdk", "claude", "claude_agent_sdk", claude_key,
                      "ANTHROPIC_API_KEY"))]


def create_team(teams_root, name: str, runner: str | None = None) -> Path:
    """Make <teams_root>/<name>/team with the starter roster; its path."""
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise StarterError("a team name is lowercase letters, digits, - and _, "
                           "starting with a letter or digit, up to 40 characters")
    team_dir = Path(teams_root).expanduser() / name / "team"
    if (team_dir / "roster.json").exists():
        raise StarterError(f"there is already a team called {name}")
    doc = starter_roster(name)
    if runner:
        doc["supervisor"] = {**(doc.get("supervisor") or {}), "runner": runner}
    (team_dir / "inbox").mkdir(parents=True, exist_ok=True)
    (team_dir / "roster.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return team_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ateam.starter", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("runtimes", help="what this machine can run")
    new = sub.add_parser("new", help="create a team")
    new.add_argument("name")
    new.add_argument("--runner", help='a runner spec, or one of the ids `runtimes` lists')
    new.add_argument("--teams-root", default=os.environ.get("ATEAM_TEAMS_ROOT") or scope.default_teams_root())
    a = ap.parse_args(argv)
    if a.cmd == "runtimes":
        for r in runtimes():
            print(f"{r['id']}: {r['label']}: " + ("ready" if r["ready"] else f"needs {r['needs']}"))
            if r["note"]:
                print(f"  {r['note']}")
        return 0
    by_id = {r["id"]: r["runner"] for r in runtimes()}
    try:
        print(create_team(a.teams_root, a.name, by_id.get(a.runner, a.runner)))
    except StarterError as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
