import re
import subprocess
import sys
from pathlib import Path

import agychat

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_semver_and_in_the_changelog():
    assert re.match(r"^\d+\.\d+\.\d+$", agychat.__version__)
    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert released[0] == agychat.__version__


def test_agyteam_is_pinned_to_a_release_not_main():
    text = (ROOT / "pyproject.toml").read_text()
    assert re.search(r"agy-team\.git@v\d+\.\d+\.\d+", text), "pin agyteam to a tag"


def test_the_command_line_reports_both_versions():
    out = subprocess.run([sys.executable, "-m", "agychat", "--version"], capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.startswith(f"agy-chat {agychat.__version__} (agyteam ")


def test_the_readme_installs_this_version():
    """0.2.0 shipped with a README that installed 0.1.0."""
    pins = re.findall(r"agy-chat\.git@v(\d+\.\d+\.\d+)", (ROOT / "README.md").read_text())
    assert pins and set(pins) == {agychat.__version__}, pins
