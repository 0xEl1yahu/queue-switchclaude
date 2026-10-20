"""Generic isolated `claude -p` caller shared by the classifier and the judge. Stdlib only.

One wrapper, two prompts, two schemas (spec section 11). Every call runs with
the isolation flags in FLAGS so the model cannot answer the message instead
of classifying it, see project context, or trigger hooks recursively.

Every failure (missing prompt file, missing binary, timeout, non-zero exit,
unparseable output, output that is not a dict) returns ``None`` so callers
fail open; the caller validates the returned dict against its own contract.

On Windows the binary is found with ``shutil.which`` (an npm install is
``claude.cmd``, which ``CreateProcess`` does not look up by bare name), the
system prompt goes by ``--system-prompt-file`` and the payload on stdin:
the command line stays short (Windows caps it at 32767 characters) and no
user text passes through ``cmd.exe``, which re-parses the arguments of a
``.cmd`` launcher. A timed-out call kills the whole process tree, since
killing ``cmd.exe`` alone would leave ``node`` holding the pipes.
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import procs

# The isolation flags, in order: no settings, no MCP servers, no tools.
FLAGS = ("--setting-sources", "", "--strict-mcp-config", "--tools", "")

# Characters ``cmd.exe`` acts on even inside an argument of a ``.cmd``
# launcher; an argument holding one is not sent through it at all.
_CMD_META = re.compile(r'[%^&|<>!\r\n]')

# After a timed-out call's tree is killed, how long to wait for its pipes.
_REAP_TIMEOUT = 2.0


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

    if procs.WINDOWS:
        result = _call_windows(claude, model, schema_json, Path(system_prompt_path), payload_json, timeout)
        if result is None:
            return None
        returncode, stdout = result
    else:
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
        returncode, stdout = proc.returncode, proc.stdout
    if returncode != 0:
        return None
    try:
        out = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(out, dict):
        return None
    structured = out.get("structured_output")
    if not isinstance(structured, dict):
        return None
    return structured


def windows_command(claude: str, model: str, schema_json: str, prompt_path: Path) -> list[str] | None:
    """The Windows argv: ``claude`` resolved on ``PATH``, prompt by file, no payload.

    ``None`` when ``claude`` resolves to a ``.cmd``/``.bat`` launcher and an
    argument holds a character ``cmd.exe`` would act on (see _CMD_META).
    """
    exe = shutil.which(claude) or claude
    cmd = [
        exe, "-p",
        "--model", model,
        *FLAGS,
        "--output-format", "json",
        "--json-schema", schema_json,
        "--system-prompt-file", str(prompt_path),
    ]
    if exe.lower().endswith((".cmd", ".bat")) and any(_CMD_META.search(arg) for arg in cmd[1:]):
        return None
    return cmd


def _call_windows(
    claude: str, model: str, schema_json: str, prompt_path: Path, payload_json: str, timeout: float,
) -> tuple[int, str] | None:
    """Run the Windows form with the payload on stdin: ``(returncode, stdout)`` or None."""
    cmd = windows_command(claude, model, schema_json, prompt_path)
    if cmd is None:
        return None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            **procs.group_kwargs(),
        )
    except (OSError, ValueError):
        return None
    try:
        stdout, _ = proc.communicate(payload_json, timeout=timeout)
    except subprocess.TimeoutExpired:
        procs.kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
            proc.communicate(timeout=_REAP_TIMEOUT)
        return None
    except (OSError, ValueError):
        procs.kill_tree(proc)
        return None
    return proc.returncode, stdout
