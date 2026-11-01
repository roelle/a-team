import subprocess
import sys

import ateam


def test_the_command_line_reports_the_version():
    out = subprocess.run([sys.executable, "-m", "ateam.chat", "--version"], capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == f"a-team {ateam.__version__}"
