import json

from qsc import transcript


def test_recent_last_n_user_and_assistant(tmp_path):
    p = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Adding the User model now."}]}},
            {"type": "user", "message": {"content": "<system-reminder>x</system-reminder>"}},
            {"type": "progress", "x": 1}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert transcript.recent(str(p), n=2) == [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Adding the User model now."}]


def test_recent_missing_path():
    assert transcript.recent("/nope.jsonl") == [] and transcript.recent(None) == []


def test_recent_truncated_first_line_ignored(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("x" * 70000 + "\n" + json.dumps({"type": "user", "message": {"content": "tail"}}) + "\n")
    assert transcript.recent(str(p), n=1, max_bytes=100) == [{"role": "user", "text": "tail"}]


def test_recent_concatenates_text_blocks_and_skips_tool_blocks(tmp_path):
    p = tmp_path / "t.jsonl"
    rows = [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "First. "},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                {"type": "text", "text": "Second."}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert transcript.recent(str(p), n=4) == [{"role": "assistant", "text": "First. Second."}]


def test_recent_skips_bad_lines_and_non_dicts(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("{not json\n[1, 2]\n" + json.dumps({"type": "user", "message": {"content": "ok then"}}) + "\n")
    assert transcript.recent(str(p)) == [{"role": "user", "text": "ok then"}]


def test_recent_directory_is_unreadable(tmp_path):
    assert transcript.recent(str(tmp_path)) == []
