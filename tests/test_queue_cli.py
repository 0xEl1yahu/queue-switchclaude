"""Task 7: scripts/queue_cli.py, the Bash side of the /queue command."""
import os
from pathlib import Path
import subprocess
import sys

from qsc import intake, paths, store
import queue_cli

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "queue_cli.py"


def ev(prompt, prompt_id="pid-1", sid="s1"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt_id": prompt_id, "prompt": prompt, "transcript_path": None}


def run_cli(data_dir, *args, cwd=None, **env):
    full_env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)}
    full_env.pop("QSC_SESSION", None)
    full_env.update(env)
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=full_env, cwd=cwd)


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
    assert r.returncode == 0 and r.stdout.strip() == "cancelled p2: email verification; p3 now after p1"
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


def test_queue_cli_approve_prints_execution_handover(data_dir):
    intake.handle(ev("add a user model"))
    with store.locked("s1") as s:
        b = s.new_task("add login", ["p1"], "pid-1"); b.status = "planned"; s.get("p1").status = "done"
        s.state = "awaiting_approval"; s.pending_task = "p2"; s.running_task = None
    r = run_cli(data_dir, "approve")
    assert r.returncode == 0 and "approved p2" in r.stdout and "Execute the plan at" in r.stdout
    s = store.load("s1"); assert s.state == "running" and s.running_task == "p2"


def test_command_file_tells_claude_to_follow_an_execution_line():
    text = (Path(__file__).resolve().parent.parent / "commands" / "queue.md").read_text()
    assert "queue-switchclaude: task" in text and "approve" in text


# M3 Task 2: the per-cwd pointer. Intake writes both the global pointer
# (newest session anywhere) and one keyed by the event's ``cwd``; the CLI
# prefers the pointer for the directory it runs in.

def test_cwd_pointer_isolates_sessions(data_dir):
    intake.handle({**ev("a", sid="s-one"), "cwd": "/proj/one"}); intake.handle({**ev("b", sid="s-two"), "cwd": "/proj/two"})
    assert queue_cli.resolve_session(None, cwd="/proj/one") == "s-one" and queue_cli.resolve_session(None, cwd="/proj/two") == "s-two"   # review focus 1
    assert queue_cli.resolve_session(None, cwd="/proj/none") == "s-two"      # global pointer: newest


def test_cwd_pointer_written_on_every_event(data_dir):
    intake.handle({**ev("add a user model", sid="s-one"), "cwd": "/proj/one"})
    assert paths.cwd_pointer_path("/proj/one").read_text().strip() == "s-one"
    assert paths.pointer_path().read_text().strip() == "s-one"
    # A later session in the same folder takes the folder over; a pass-through counts.
    intake.handle({**ev("ok", sid="s-three"), "cwd": "/proj/one"})
    assert paths.cwd_pointer_path("/proj/one").read_text().strip() == "s-three"
    # An event without a cwd still refreshes the global pointer and leaves the cwd pointers alone.
    intake.handle(ev("later: x", sid="s-four"))
    assert paths.pointer_path().read_text().strip() == "s-four"
    assert paths.cwd_pointer_path("/proj/one").read_text().strip() == "s-three"


def test_explicit_session_and_env_beat_cwd_pointer(data_dir, monkeypatch):
    intake.handle({**ev("a", sid="s-one"), "cwd": "/proj/one"})
    assert queue_cli.resolve_session("s-x", cwd="/proj/one") == "s-x"
    monkeypatch.setenv("QSC_SESSION", "s-env")
    assert queue_cli.resolve_session(None, cwd="/proj/one") == "s-env"
    monkeypatch.delenv("QSC_SESSION")
    assert queue_cli.resolve_session(None, cwd="/proj/one") == "s-one"


def test_resolve_session_defaults_to_the_current_directory(data_dir, monkeypatch, tmp_path):
    here = tmp_path / "here"; here.mkdir()
    intake.handle({**ev("a", sid="s-here"), "cwd": str(here)}); intake.handle({**ev("b", sid="s-elsewhere"), "cwd": "/proj/elsewhere"})
    monkeypatch.chdir(here)
    assert queue_cli.resolve_session(None) == "s-here"


def test_queue_cli_subprocess_uses_the_directory_it_runs_in(data_dir, tmp_path):
    one = tmp_path / "one"; two = tmp_path / "two"; one.mkdir(); two.mkdir()
    intake.handle({**ev("add a user model", sid="s-one"), "cwd": str(one)}); intake.handle({**ev("later: email verification", sid="s-one"), "cwd": str(one)})
    intake.handle({**ev("other work", sid="s-two"), "cwd": str(two)}); intake.handle({**ev("later: write docs", sid="s-two"), "cwd": str(two)})
    r = run_cli(data_dir, "list", cwd=str(one))
    assert r.returncode == 0 and "email verification" in r.stdout and "write docs" not in r.stdout
    r = run_cli(data_dir, "list", cwd=str(two))
    assert r.returncode == 0 and "write docs" in r.stdout and "email verification" not in r.stdout


def test_cli_finds_the_session_through_a_symlinked_cwd(data_dir, tmp_path):
    # Intake saw the folder through a symlink; the CLI runs in the real path.
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"; link.symlink_to(real)
    intake.handle({**ev("a", sid="s-link"), "cwd": str(link)})
    intake.handle({**ev("b", sid="s-other"), "cwd": "/proj/other"})          # newest: the global pointer
    assert queue_cli.resolve_session(None, cwd=str(real)) == "s-link"
    assert queue_cli.resolve_session(None, cwd=str(link)) == "s-link"


def test_cli_in_a_subdirectory_resolves_to_the_enclosing_session(data_dir, tmp_path):
    # The Bash tool keeps its cwd across calls: after a ``cd packages/x`` the
    # slash command runs below the project root. The nearest enclosing
    # folder's session answers, never the newest session anywhere.
    proj = tmp_path / "proj"; sub = proj / "packages" / "x"; sub.mkdir(parents=True)
    intake.handle({**ev("a", sid="s-proj"), "cwd": str(proj)})
    intake.handle({**ev("b", sid="s-other"), "cwd": "/proj/other"})
    assert queue_cli.resolve_session(None, cwd=str(sub)) == "s-proj"
    # A session of its own in the subdirectory wins over the parent's.
    intake.handle({**ev("c", sid="s-sub"), "cwd": str(sub)})
    assert queue_cli.resolve_session(None, cwd=str(sub / "deeper")) == "s-sub"
    assert queue_cli.resolve_session(None, cwd=str(proj)) == "s-proj"
    r = run_cli(data_dir, "list", cwd=str(sub))
    assert r.returncode == 0 and "c" in r.stdout


# --- 2026-10-20 review fix 5: the approve handover and the turn it runs in ---

def _awaiting_approval():
    intake.handle(ev("add a user model"))
    with store.locked("s1") as s:
        b = s.new_task("add login", ["p1"], "pid-1"); b.status = "planned"; s.get("p1").status = "done"
        s.state = "awaiting_approval"; s.pending_task = "p2"; s.running_task = None; s.interrupt = True


def test_queue_cli_approve_puts_the_handover_on_its_own_line(data_dir):
    _awaiting_approval()
    r = run_cli(data_dir, "approve")
    lines = r.stdout.splitlines()
    assert any(l.startswith("queue-switchclaude: task p2 is approved") for l in lines)


def test_queue_cli_approve_adopts_the_turns_prompt_id_and_clears_interrupt(data_dir):
    _awaiting_approval()
    # The slash command's own prompt reached intake (a pass-through) before Claude ran the CLI.
    intake.handle(ev("/compact", prompt_id="pid-7"))
    run_cli(data_dir, "approve")
    s = store.load("s1"); assert s.current_prompt_id == "pid-7" and s.interrupt is False and s.state == "running"
    # A message typed during the execution is mid-turn, not an Esc.
    out = intake.handle(ev("later: add logout", prompt_id="pid-7"))
    s = store.load("s1"); assert out["decision"] == "block" and s.get("p2").status == "running"


def test_queue_cli_approve_without_a_recorded_prompt_keeps_the_current_one(data_dir):
    _awaiting_approval()
    paths.last_prompt_path("s1").unlink(missing_ok=True)
    run_cli(data_dir, "approve")
    s = store.load("s1"); assert s.current_prompt_id == "pid-1" and s.interrupt is False
