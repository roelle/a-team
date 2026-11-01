#!/usr/bin/env bash
# Install the a-team plugin into the Antigravity CLI.
#
# The installed plugin is self-contained: the MCP servers it mounts are pure
# standard library, so they are copied in alongside the manifest and run under
# a bare system python3. No virtualenv, no third-party packages, and nothing
# breaks if this repo later moves or is deleted.
#
# Agent identity is NOT baked in: the servers inherit ATEAM_AGENT from the
# agy process, so one install serves every agent on every team.
set -euo pipefail

# plugin/ in a checkout, or ateam/plugin/ in an installed package: either way
# the ateam package is what sits beside it.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATEAM_HOME="$(cd "$HERE/.." && pwd)"
[ -d "$ATEAM_HOME/ateam" ] || ATEAM_HOME="$(cd "$HERE/../.." && pwd)"
PYTHON="${ATEAM_PYTHON:-$(command -v python3)}"
PLUGIN_SRC="$HERE/a-team"
PLUGIN_DST="${ATEAM_PLUGIN_DIR:-$HOME/.gemini/antigravity-cli/plugins/a-team}"
TEAM="${ATEAM_TEAM:-default}"
TEAMS_ROOT="${ATEAM_TEAMS_ROOT:-$HOME/a-teams}"
DURABLE="${ATEAM_DURABLE_DIR:-$TEAMS_ROOT/$TEAM}"

# Modules the MCP servers need, derived from the source instead of listed here.
# The list used to be written out by hand and it drifted the first time someone
# added an import: two modules that mcp_bus imports *inside a function* were
# never copied, so those tools raised ImportError on the installed copy while
# every test in the repo passed. ateam/install_check.py walks both kinds of
# edge -- imports at any nesting depth, and the "ateam.x:Class" spec strings
# the seams resolve at runtime -- and excludes anything that would drag in a
# third-party package, which is the property this install actually depends on.
mapfile -t PURE_MODULES < <(PYTHONPATH="$ATEAM_HOME" "$PYTHON" \
    -m ateam.install_check --list) || true
[ ${#PURE_MODULES[@]} -gt 0 ] || {
  echo "ERROR: could not derive the module list (ateam.install_check failed)"
  exit 1
}

echo "python      : $PYTHON"
echo "plugin dest : $PLUGIN_DST"
echo "team        : $TEAM"
echo "durable dir : $DURABLE"

[ -x "$PYTHON" ] || { echo "ERROR: python3 not found (set ATEAM_PYTHON)"; exit 1; }

mkdir -p "$PLUGIN_DST/ateam" "$DURABLE/team/inbox" "$DURABLE/agents"
cp -r "$PLUGIN_SRC/." "$PLUGIN_DST/"
for m in "${PURE_MODULES[@]}"; do
  cp "$ATEAM_HOME/ateam/$m" "$PLUGIN_DST/ateam/$m"
done

sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__ATEAM_HOME__|$PLUGIN_DST|g" \
    "$HERE/mcp_config.template.json" > "$PLUGIN_DST/mcp_config.json"
sed -e "s|__PYTHON__|$PYTHON|g" \
    "$HERE/hooks.template.json" > "$PLUGIN_DST/hooks.json"

if [ ! -f "$DURABLE/team/roster.json" ]; then
  sed "s/(\$TEAM)/($TEAM)/" "$HERE/starter_roster.json" > "$DURABLE/team/roster.json"
  echo "seeded roster: $DURABLE/team/roster.json"
fi

# Prove the install actually works rather than merely looking right: exercise it
# using only the installed copy, with this repo off the path. Importing the
# servers is not enough — the transport is resolved lazily at runtime, so a
# module missing from the copy would slip through. Instantiate it and make a
# real call.
#
# Neutral cwd matters as much as the path: run from the repo and `./ateam/`
# shadows the install, so the check passes by testing the wrong copy.
( cd / && env -u ATEAM_RUNNER PYTHONPATH="$PLUGIN_DST" ATEAM_TEAM_DIR="$DURABLE/team" "$PYTHON" -c "
import json, ateam.mcp_memory, ateam.mcp_bus, ateam.mcp_self, ateam.mcp_tasks, ateam.runner_host, ateam.hook_pre_tool_use, ateam.hook_stop, ateam.models
from ateam.transport import load as load_bus
from ateam.memory import load as load_memory
from ateam.runner import load as load_runner
from ateam.observer import load as load_observer
from ateam import tasks as load_tasks
load_bus('installer-selftest').teammates()      # forces roster parsing
load_memory('installer-selftest').index()       # forces memory store load
load_runner()                                   # forces runner load
load_observer()                                 # forces observer load
load_tasks.due('$DURABLE/team')                 # forces the task log to load
json.load(open('$PLUGIN_DST/mcp_config.json')); json.load(open('$PLUGIN_DST/plugin.json'))
print('verified: servers import; transport, memory store, runner, observer, and tasks all load')" )

# And that every module the installed sources import is actually there. The
# import above only executes top-level imports; the two modules that went
# missing were imported inside functions, so this reads the source instead.
( cd / && PYTHONPATH="$PLUGIN_DST" "$PYTHON" -m ateam.install_check --verify "$PLUGIN_DST" )

# The plugin from before the rename mounts servers of its own beside these, and
# its team may still be running, so it is named here and left for you to remove.
OLD_PLUGIN="$(dirname "$PLUGIN_DST")/agy-team"
if [ -d "$OLD_PLUGIN" ]; then
  echo
  echo "NOTE: the agy-team plugin is also installed, at $OLD_PLUGIN."
  echo "      Agents would see both sets of tools. Once nothing here runs on"
  echo "      agy-team, remove it: agy plugin uninstall agy-team"
fi

# The review gate shells out to pytest, which a dependency-free install does
# not have. Say so here rather than letting the first review come back as
# "proof_file did not pass" — a check that cannot run must not look like a
# verdict, and an agent reading that rewrites a proof that was already correct.
if ! ( cd / && "$PYTHON" -c "import pytest" >/dev/null 2>&1 ); then
  echo
  echo "NOTE: $PYTHON cannot import pytest, so record_review cannot run a proof"
  echo "      file with it. Set ATEAM_PROOF_PYTHON to an interpreter that can"
  echo "      (a project venv is the usual answer) before the team reviews"
  echo "      anything. The gate reports this as an environment failure and"
  echo "      records nothing, rather than rejecting the work."
fi

cat <<NOTE

Installed. Run an agent so its memory and mail are attributed correctly:

    $PYTHON -m ateam.session tpm

That joins the conversation tpm is actually working in, with its identity and
role already set. Do not use 'agy --agent tpm': that mechanism strips every
builtin tool, so the agent cannot write a file.

Or let the team run itself — teammates are woken when they get mail, so one
instruction cascades without you checking anything:

    $PYTHON -m ateam.supervisor --say "tpm: <your task>"    # until idle
    $PYTHON -m ateam.supervisor --daemon                    # stay reactive
    $PYTHON -m ateam.supervisor --status                    # who has mail

Teams are isolated — separate roster, bus, and memory, no cross-talk:

    ATEAM_TEAM=team-b bash plugin/install.sh     # seed another team
    ATEAM_TEAM=team-b $PYTHON -m ateam.session tpm

Or point somewhere explicit (useful if your backups target a specific tree):

    ATEAM_DURABLE_DIR=~/my-agents/my-agent-team-A $PYTHON -m ateam.session tpm

'$PYTHON -m ateam.scope' prints what currently resolves where.
NOTE
