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
    assert m["version"] == "0.3.0"
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


# Task 6 (M3): installable from GitHub, CI, version.

def test_marketplace_manifest_lists_this_plugin():
    m = json.load(open(ROOT / ".claude-plugin" / "marketplace.json"))
    assert m["name"] == "queue-switchclaude" and m["plugins"][0]["source"] == "." and m["plugins"][0]["name"] == "queue-switchclaude"


def test_plugin_version_and_links():
    m = json.load(open(ROOT / ".claude-plugin" / "plugin.json"))
    assert m["version"] == "0.3.0" and m["repository"].startswith("https://github.com/0xEl1yahu/queue-switchclaude") and m["license"] == "MIT"


def test_readme_has_marketplace_install_line():
    text = (ROOT / "README.md").read_text()
    assert "/plugin marketplace add 0xEl1yahu/queue-switchclaude" in text and (ROOT / "LICENSE").exists() and (ROOT / ".github" / "workflows" / "test.yml").exists()


def test_marketplace_manifest_owner_and_description():
    m = json.load(open(ROOT / ".claude-plugin" / "marketplace.json"))
    assert m["owner"]["name"] == "0xEl1yahu"
    assert m["plugins"][0]["description"].startswith("Sorts messages typed mid-run into steer / queue / amend / split")


def test_plugin_manifest_homepage_matches_repository():
    m = json.load(open(ROOT / ".claude-plugin" / "plugin.json"))
    assert m["homepage"] == "https://github.com/0xEl1yahu/queue-switchclaude"
    assert m["repository"] == "https://github.com/0xEl1yahu/queue-switchclaude"


def test_license_is_mit():
    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("MIT License") and "0xEl1yahu" in text


def test_readme_keeps_plugin_dir_line_for_development():
    text = (ROOT / "README.md").read_text()
    assert "## Install" in text and "## Install for development" in text
    assert text.index("/plugin marketplace add") < text.index("## Install for development")
    assert "claude --plugin-dir" in text


def test_ci_workflow_matrix_and_test_command():
    text = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "pull_request" in text and "push" in text and "ubuntu-latest" in text
    for version in ('"3.10"', '"3.12"', '"3.14"'):
        assert version in text
    assert "python -m pip install pytest" in text and "python -m pytest -q" in text
