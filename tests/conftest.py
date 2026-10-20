import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

WINDOWS = os.name == "nt"

# The fake `claude` CLI: a sh script on POSIX, a .cmd launcher (the shape of
# an npm install) around a Python fake on Windows.
FAKE_CLAUDE = str(ROOT / "tests" / "fakes" / ("claude.cmd" if WINDOWS else "claude"))

# A shell-ready way to start this interpreter in a verify command:
# ``python3`` is not on PATH on every Windows runner.
PY = f'"{sys.executable}"'


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the plugin's data directory at a temp dir for the test."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SHADOW", raising=False)
    monkeypatch.chdir(ROOT)
    return tmp_path
