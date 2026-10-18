import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the plugin's data directory at a temp dir for the test."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_SHADOW", raising=False)
    monkeypatch.chdir(ROOT)
    return tmp_path
