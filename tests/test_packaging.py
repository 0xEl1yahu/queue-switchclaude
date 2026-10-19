"""Task 8: the repository is a loadable Claude Code plugin."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EVENTS = ["UserPromptSubmit", "Stop", "PreToolUse"]


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
    }


# --- M3 Task 4: the interrupt hook costs one launch per tool call -------------

def test_interrupt_hook_registered_on_pretooluse_only():
    h = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    assert "PostToolUse" not in h and h["PreToolUse"][0]["hooks"][0]["args"][0].endswith("interrupt.py")


def test_hooks_json_registers_exactly_three_events():
    h = json.load(open(ROOT / "hooks" / "hooks.json"))["hooks"]
    assert set(h) == {"UserPromptSubmit", "Stop", "PreToolUse"}
    assert all(len(h[e]) == 1 and len(h[e][0]["hooks"]) == 1 for e in h)


def test_plugin_manifest_declares_shadow():
    m = json.load(open(ROOT / ".claude-plugin" / "plugin.json"))
    assert m["name"] == "queue-switchclaude" and m["userConfig"]["shadow"]["type"] == "boolean"
    assert m["version"] == "0.1.0"
    assert m["author"]["name"] == "0xEl1yahu"
    assert m["userConfig"]["shadow"]["default"] is False


def test_command_file_present_and_wired():
    text = (ROOT / "commands" / "queue.md").read_text()
    assert "queue_cli.py" in text and "allowed-tools: Bash(python3 *)" in text


def test_command_file_frontmatter_and_script_exist():
    text = (ROOT / "commands" / "queue.md").read_text()
    assert text.startswith("---\n") and "description:" in text and "argument-hint:" in text
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/queue_cli.py" in text and "$ARGUMENTS" in text
    assert (ROOT / "scripts" / "queue_cli.py").exists()


def test_command_file_passes_plugin_data_dir_to_the_cli():
    text = (ROOT / "commands" / "queue.md").read_text()
    assert 'CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}"' in text and "Bash(CLAUDE_PLUGIN_DATA=*)" in text
