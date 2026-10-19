import json
import os
import subprocess
import sys
from pathlib import Path

from qsc import paths

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "compare.py"
FAKES = ROOT / "tests" / "fakes"


def run_compare(data_dir, job, **env):
    jobs = data_dir / "compare"; jobs.mkdir(exist_ok=True)
    path = jobs / "job-1.json"; path.write_text(json.dumps(job))
    full = {**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir), "PATH": f"{FAKES}:{os.environ.get('PATH', '')}"}
    full.update(env)
    r = subprocess.run([sys.executable, str(SCRIPT), "s1", str(path)], capture_output=True, text=True, env=full)
    return r, path


def test_compare_logs_the_model_answer_and_deletes_the_job(data_dir):
    out = {"structured_output": {"label": "steer", "confidence": 0.95, "interrupt": False, "parked": False,
                                 "depends_on": [], "target": None, "note": None, "parts": []}}
    r, path = run_compare(data_dir, {"payload": {"message": "x"}, "message": "later: x", "rule": "queue_marker",
                                     "rule_label": "queue", "prompt_id": "pid"}, FAKE_CLAUDE_OUTPUT=json.dumps(out))
    assert r.returncode == 0 and r.stdout == "" and not path.exists()
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["event"] == "shadow_compare" and row["rule_label"] == "queue" and row["classifier"]["label"] == "steer"
    assert row["latency_ms"] >= 0 and row["message"] == "later: x"


def test_compare_logs_null_when_the_model_fails(data_dir):
    r, _ = run_compare(data_dir, {"payload": {"message": "x"}, "message": "m", "rule": "queue_marker",
                                  "rule_label": "queue", "prompt_id": None}, FAKE_CLAUDE_OUTPUT="not json")
    assert r.returncode == 0
    row = [json.loads(l) for l in paths.log_path("s1").read_text().splitlines()][-1]
    assert row["event"] == "shadow_compare" and row["classifier"] is None


def test_compare_swallows_bad_arguments(data_dir):
    r = subprocess.run([sys.executable, str(SCRIPT), "s1", "/nonexistent.json"], capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)})
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
