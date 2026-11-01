"""Install the agy plugin from a plain install, with no checkout.

    python -m agyteam.install_plugin

Runs the installer that ships inside the package, plugin/install.sh. It copies
the plugin to ~/.gemini/antigravity-cli/plugins/agy-team, or AGYTEAM_PLUGIN_DIR,
and checks the copy works. Only the agy CLI runtime needs this.
"""
from __future__ import annotations

import subprocess
import sys

from .starter import plugin_dir


def install() -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(plugin_dir() / "install.sh")],
                          capture_output=True, text=True, check=False)


def main() -> int:
    r = install()
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
