"""Task 8: the repository is a loadable Claude Code plugin."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EVENTS = ["UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse"]


def test_hooks_json_uses_exec_form_and_existing_scripts():
    h = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    for event in EVENTS:
        hook = h[event][0]["hooks"][0]
        assert hook["type"] == "command"
        assert hook["command"] == "python3" and hook["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/scripts/")
        assert (ROOT / hook["args"][0].replace("${CLAUDE_PLUGIN_ROOT}/", "")).exists()
        assert isinstance(hook["timeout"], int) and hook["timeout"] > 0


def test_hooks_json_maps_each_event_to_its_script():
    h = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    script = {e: h[e][0]["hooks"][0]["args"][0].rsplit("/", 1)[1] for e in EVENTS}
    assert script == {
        "UserPromptSubmit": "intake.py",
        "Stop": "dispatch.py",
        "PreToolUse": "interrupt.py",
        "PostToolUse": "interrupt.py",
    }


def test_plugin_manifest_declares_shadow():
    m = json.load(open(ROOT / ".claude-plugin" / "plugin.json"))
    assert m["name"] == "queue-switchclaude" and m["userConfig"]["shadow"]["type"] == "boolean"
    assert m["version"] == "0.1.0"
    assert m["author"]["name"] == "0xEl1yahu"
    assert m["userConfig"]["shadow"]["default"] is False
