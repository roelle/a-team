"""Starting a team with no checkout: what a first run needs.

Asked for as a one-command install. The plugin lived outside the package, and
the starter roster inside a shell heredoc, so a plain install could do neither.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import actions, install_plugin, runner, starter  # noqa: E402

KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for k in (*KEYS, "ATEAM_AGENT", "ATEAM_RUNNER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(starter, "AGY_SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(starter.shutil, "which", lambda name: None)


def row(rid):
    return next(r for r in starter.runtimes() if r["id"] == rid)


def test_a_new_team_has_the_starter_agents_and_its_runtime(tmp_path):
    td = starter.create_team(tmp_path, "Acme-1", "ateam.runner_claude:ClaudeRunner")
    assert td == tmp_path / "acme-1" / "team" and (td / "inbox").is_dir()
    doc = json.loads((td / "roster.json").read_text())
    assert [a["name"] for a in doc["agents"]] == ["manager", "tpm", "coder", "syseng", "qa"]
    assert "(acme-1)" in doc["mission"]
    assert runner.team_runner(td)[0] == "ateam.runner_claude:ClaudeRunner"


@pytest.mark.parametrize("bad", ["", "../up", "a/b", "-x", "has space", "x" * 41])
def test_a_bad_name_is_refused_and_writes_nothing(tmp_path, bad):
    with pytest.raises(starter.StarterError):
        starter.create_team(tmp_path / "root", bad)
    assert not (tmp_path / "root").exists()


def test_a_team_is_never_overwritten(tmp_path):
    td = starter.create_team(tmp_path, "t")
    (td / "roster.json").write_text('{"agents": []}')
    with pytest.raises(starter.StarterError, match="already a team"):
        starter.create_team(tmp_path, "t")
    assert (td / "roster.json").read_text() == '{"agents": []}'


def test_nothing_is_ready_on_a_bare_machine_and_each_says_what_it_needs():
    rows = starter.runtimes()
    assert not any(r["ready"] for r in rows)
    assert "agy command" in row("agy")["needs"]
    assert "GEMINI_API_KEY" in row("sdk")["needs"]
    assert "ANTHROPIC_API_KEY" in row("claude")["needs"]
    assert "codex command" in row("codex")["needs"] and "OPENAI_API_KEY" in row("codex")["needs"]
    assert "opencode command" in row("opencode")["needs"]


def test_a_key_makes_a_runtime_ready_and_is_never_repeated(monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key-value")
    assert row("claude")["ready"]
    assert "not-a-real-key-value" not in json.dumps(starter.runtimes())


def test_a_missing_package_says_how_to_add_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(starter, "_has", lambda module: False)
    assert 'a-team[claude]' in row("claude")["needs"]


def test_a_package_whose_parent_is_missing_counts_as_missing():
    assert starter._has("no_such_parent_package.child") is False


def test_agy_says_when_agents_would_stop_at_approval(monkeypatch):
    monkeypatch.setattr(starter.shutil, "which", lambda name: "/usr/bin/agy")
    assert row("agy")["ready"] and "toolPermission" in row("agy")["note"]
    starter.AGY_SETTINGS.write_text('{"toolPermission": "always-proceed"}')
    assert row("agy")["note"] == ""


def test_actions_refuse_a_runtime_that_is_not_ready(tmp_path):
    with pytest.raises(actions.OperatorError, match="not ready"):
        actions.create_team(tmp_path, "t", "agy")
    assert not (tmp_path / "t").exists()


def test_an_agent_cannot_create_a_team(tmp_path, monkeypatch):
    monkeypatch.setenv("ATEAM_AGENT", "coder")
    with pytest.raises(actions.OperatorError, match="operator"):
        actions.create_team(tmp_path, "t", "agy")


def test_a_failed_plugin_install_stops_before_the_team_is_made(tmp_path, monkeypatch):
    monkeypatch.setattr(starter.shutil, "which", lambda name: "/usr/bin/agy")
    monkeypatch.setattr(install_plugin, "install", lambda: subprocess.CompletedProcess(
        [], 1, stdout="", stderr="no python3"))
    with pytest.raises(actions.OperatorError, match="no python3"):
        actions.create_team(tmp_path, "t", "agy")
    assert not (tmp_path / "t").exists()


def test_the_installer_works_from_its_module(tmp_path):
    env = {**os.environ, "ATEAM_PLUGIN_DIR": str(tmp_path / "plugin"),
           "ATEAM_TEAMS_ROOT": str(tmp_path / "teams"), "ATEAM_TEAM": "t"}
    env.pop("ATEAM_DURABLE_DIR", None)
    env.pop("ATEAM_TEAM_DIR", None)
    r = subprocess.run([sys.executable, "-m", "ateam.install_plugin"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stderr + r.stdout
    assert (tmp_path / "plugin" / "hooks.json").exists()
    doc = json.loads((tmp_path / "teams" / "t" / "team" / "roster.json").read_text())
    assert doc["mission"].endswith("(t).")


def test_the_package_carries_the_plugin():
    text = (ROOT / "pyproject.toml").read_text()
    assert '"ateam.plugin" = "plugin"' in text
    assert (starter.plugin_dir() / "install.sh").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
