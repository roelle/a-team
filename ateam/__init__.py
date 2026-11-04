"""a-team: a team of persistent, learning agents."""

# The single source of the version: pyproject.toml reads it from here, and
# `python -m ateam.supervisor --version` prints it. See RELEASING.md.
import os

__version__ = "4.9.0"


def _adopt_old_names() -> None:
    """Settings from before the rename still count: AGYTEAM_X is ATEAM_X, and
    the app's two AGYCHAT_ names. A value set under the new name wins."""
    for key in [k for k in os.environ if k.startswith("AGYTEAM_")]:
        os.environ.setdefault("ATEAM_" + key[len("AGYTEAM_"):], os.environ[key])
    for old, new in (("AGYCHAT_TOKEN", "ATEAM_TOKEN"), ("AGYCHAT_PEOPLE", "ATEAM_PEOPLE")):
        if old in os.environ:
            os.environ.setdefault(new, os.environ[old])


_adopt_old_names()
