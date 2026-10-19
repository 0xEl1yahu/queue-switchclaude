"""Generic isolated `claude -p` caller shared by the classifier and the judge. Stdlib only.

One wrapper, two prompts, two schemas (spec section 11). Every call runs with
the isolation flags in FLAGS so the model cannot answer the message instead
of classifying it, see project context, or trigger hooks recursively.

Every failure (missing prompt file, missing binary, timeout, non-zero exit,
unparseable output, output that is not a dict) returns ``None`` so callers
fail open; the caller validates the returned dict against its own contract.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

# The isolation flags, in order: no settings, no MCP servers, no tools.
FLAGS = ("--setting-sources", "", "--strict-mcp-config", "--tools", "")


def call(
    system_prompt_path: Path,
    schema: dict,
    payload: dict,
    timeout: float = 15.0,
    claude: str = "claude",
    model: str = "haiku",
) -> dict | None:
    """Run `claude -p` with ``schema`` and ``payload``; return ``structured_output``.

    ``None`` on any failure. The command is built verbatim:
    ``claude -p --model <model> <FLAGS> --output-format json --json-schema <schema>
    --system-prompt <prompt> <payload>``.
    """
    try:
        prompt = Path(system_prompt_path).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return None
    try:
        schema_json = json.dumps(schema)
        payload_json = json.dumps(payload)
    except (TypeError, ValueError):
        return None

    cmd = [
        claude, "-p",
        "--model", model,
        *FLAGS,
        "--output-format", "json",
        "--json-schema", schema_json,
        "--system-prompt", prompt,
        payload_json,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    try:
        out = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(out, dict):
        return None
    structured = out.get("structured_output")
    if not isinstance(structured, dict):
        return None
    return structured
