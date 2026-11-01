"""The version is written once, and the changelog and tag agree with it.

Downstream asked for releases with semantic versioning; a release whose
package, changelog and tag say three different things is not one.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ateam  # noqa: E402

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_the_version_is_semver():
    assert SEMVER.match(ateam.__version__), ateam.__version__


def test_pyproject_reads_it_rather_than_repeating_it():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in text and 'attr = "ateam.__version__"' in text
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    assert not re.search(r'^version\s*=', project, re.M), "[project] repeats the version"


def test_the_changelog_has_an_entry_for_it():
    text = (ROOT / "CHANGELOG.md").read_text()
    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}", text, re.M)
    assert released and released[0] == ateam.__version__, \
        f"newest changelog release {released[:1]} != {ateam.__version__}"
    assert "## [Unreleased]" in text


def test_a_tag_for_this_version_points_at_a_commit_that_has_it():
    """If vX.Y.Z exists, the ateam/__init__.py at that tag carries X.Y.Z."""
    tag = f"v{ateam.__version__}"
    p = subprocess.run(["git", "-C", str(ROOT), "show", f"{tag}:ateam/__init__.py"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip(f"{tag} is not tagged yet (or this is not a git checkout)")
    assert f'__version__ = "{ateam.__version__}"' in p.stdout


def test_the_install_instructions_pin_this_version():
    """A release that does not move these tells people to install the one
    before it. a-team 0.2.0 shipped that way."""
    for doc in ("README.md", "RELEASING.md"):
        pins = re.findall(r"a-team\.git@v(\d+\.\d+\.\d+)", (ROOT / doc).read_text())
        assert pins and set(pins) == {ateam.__version__}, (doc, pins)


def test_the_command_line_reports_it():
    out = subprocess.run([sys.executable, "-m", "ateam.supervisor", "--version"],
                         capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert out.stdout.strip() == f"ateam {ateam.__version__}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
