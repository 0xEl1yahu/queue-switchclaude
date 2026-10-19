"""Task 7: scripts/queue_cli.py, the Bash side of the /queue command."""
import os
from pathlib import Path
import subprocess
import sys

from qsc import intake, paths, store

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "queue_cli.py"


def ev(prompt, prompt_id="pid-1", sid="s1"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt_id": prompt_id, "prompt": prompt, "transcript_path": None}


def run_cli(data_dir, *args, **env):
    full_env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)}
    full_env.pop("QSC_SESSION", None)
    full_env.update(env)
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=full_env)


def test_queue_cli_uses_pointer(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "queue_cli.py"), "list"], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert r.returncode == 0 and "p2" in r.stdout


def test_queue_cli_no_session(data_dir):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "queue_cli.py"), "list"], capture_output=True, text=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert r.returncode == 0 and r.stdout.strip() == "no session found"


def test_intake_writes_pointer_on_every_event(data_dir):
    assert intake.handle(ev("add a user model", sid="abc")) is None
    assert paths.pointer_path() == data_dir / "last_session"
    assert paths.pointer_path().read_text().strip() == "abc"
    # A pass-through non-task still refreshes the pointer to the latest session.
    assert intake.handle(ev("ok", sid="later-session")) is None
    assert paths.pointer_path().read_text().strip() == "later-session"


def test_queue_cli_session_flag_beats_pointer(data_dir):
    intake.handle(ev("add a user model", sid="s1")); intake.handle(ev("later: email verification", sid="s1"))
    intake.handle(ev("other work", sid="s2"))
    assert paths.pointer_path().read_text().strip() == "s2"
    r = run_cli(data_dir, "--session", "s1", "list")
    assert r.returncode == 0 and "p2" in r.stdout and "email verification" in r.stdout
    r = run_cli(data_dir, "--session=s1", "list")
    assert r.returncode == 0 and "p2" in r.stdout


def test_queue_cli_env_session_beats_pointer(data_dir):
    intake.handle(ev("add a user model", sid="s1")); intake.handle(ev("later: email verification", sid="s1"))
    intake.handle(ev("other work", sid="s2"))
    r = run_cli(data_dir, "list", QSC_SESSION="s1")
    assert r.returncode == 0 and "p2" in r.stdout


def test_queue_cli_empty_command_lists(data_dir):
    intake.handle(ev("add a user model"))
    r = run_cli(data_dir)
    assert r.returncode == 0 and "p1" in r.stdout and "running" in r.stdout


def test_queue_cli_edits_store_under_lock(data_dir):
    intake.handle(ev("add a user model")); intake.handle(ev("later: email verification")); intake.handle(ev("maybe later we can add social login"))
    r = run_cli(data_dir, "cancel", "p2")
    assert r.returncode == 0 and r.stdout.strip() == "cancelled p2: email verification"
    assert store.load("s1").get("p2").status == "cancelled"
    r = run_cli(data_dir, "promote", "p3")
    assert r.returncode == 0 and r.stdout.strip() == "promoted p3 to the queue"
    assert store.load("s1").get("p3").status == "queued"
    r = run_cli(data_dir, "pause")
    assert r.returncode == 0 and r.stdout.strip() == "queue paused" and store.load("s1").paused is True


def test_queue_cli_unknown_command_prints_help(data_dir):
    intake.handle(ev("add a user model"))
    r = run_cli(data_dir, "frobnicate")
    assert r.returncode == 0 and "cancel pN" in r.stdout


def test_queue_cli_bad_pointer_is_no_session(data_dir):
    paths.pointer_path().parent.mkdir(parents=True, exist_ok=True)
    paths.pointer_path().write_text("   \n")
    r = run_cli(data_dir, "list")
    assert r.returncode == 0 and r.stdout.strip() == "no session found"
