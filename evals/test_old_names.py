"""What still works from before the rename, so a team can move when it likes."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import policy, scope  # noqa: E402
from ateam.names import current_spec  # noqa: E402
from ateam.runner import load as load_runner  # noqa: E402


def run(code, **env):
    keep = {k: v for k, v in os.environ.items() if not k.startswith(("ATEAM_", "AGYTEAM_"))}
    return subprocess.run([sys.executable, "-c", code], env={**keep, **env}, cwd=ROOT,
                          capture_output=True, text=True, check=True, timeout=30).stdout.strip()


SHOW = "import ateam, os; print(os.environ.get('ATEAM_TEAM_DIR'))"


def test_an_old_variable_counts():
    assert run(SHOW, AGYTEAM_TEAM_DIR="/old") == "/old"


def test_the_new_name_wins():
    assert run(SHOW, AGYTEAM_TEAM_DIR="/old", ATEAM_TEAM_DIR="/new") == "/new"


def test_the_apps_old_variable_counts():
    code = "import ateam, os; print(os.environ.get('ATEAM_PEOPLE'))"
    assert run(code, AGYCHAT_PEOPLE="/p.json") == "/p.json"


def test_an_old_spec_loads_the_same_runner():
    assert current_spec("agyteam.runner_agy:AgyRunner") == "ateam.runner_agy:AgyRunner"
    assert current_spec("mycompany.runner:Thing") == "mycompany.runner:Thing"
    assert type(load_runner("agyteam.runner_agy:AgyRunner")).__name__ == "AgyRunner"


def test_existing_teams_are_found_until_the_new_home_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert scope.default_teams_root() == "~/a-teams"
    (tmp_path / "agy-teams").mkdir()
    assert scope.default_teams_root() == "~/agy-teams"
    (tmp_path / "a-teams").mkdir()
    assert scope.default_teams_root() == "~/a-teams"


def test_an_existing_shared_directory_stays_in_use(tmp_path):
    assert scope._shared_default(tmp_path).name == ".a-team-shared"
    (tmp_path / ".agy-team-shared").mkdir()
    assert scope._shared_default(tmp_path).name == ".agy-team-shared"


def test_an_old_roster_allows_the_renamed_server(tmp_path):
    (tmp_path / "roster.json").write_text(json.dumps({"agents": [
        {"name": "coder", "role": "x", "allowed_mcp_servers": ["agyteam_bus"]}]}))
    assert policy.check_tool_policy("coder", "mcp_tool", {"ServerName": "ateam_bus"},
                                    team_dir=tmp_path) in (None, "")
    refused = policy.check_tool_policy("coder", "mcp_tool", {"ServerName": "other"},
                                       team_dir=tmp_path)
    assert refused


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
