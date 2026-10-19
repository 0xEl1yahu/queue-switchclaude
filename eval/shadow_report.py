#!/usr/bin/env python3
"""Shadow report: how often the rules decide alone, and how often the model disagrees.

Reads every ``log/*.jsonl`` under the plugin data dir (``$CLAUDE_PLUGIN_DATA``,
else ``~/.claude/queue-switchclaude``) and prints:

- how many intake decisions the rules made alone and how many went to the model
- the rule/model disagreement rate where both answers are logged (shadow mode
  with the ``shadow_compare`` option on, M1 Task 10)
- the mean wall-clock latency of the ``claude -p`` call where ``latency_ms`` is logged
- the ten most recent disagreements with their message text

Usage: ``python3 eval/shadow_report.py [--since 7d]`` (``s``, ``m``, ``h``, ``d``, ``w``).

Stdlib only. A log line that is not JSON, or not an intake decision, is skipped.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from qsc import paths  # noqa: E402

RECENT = 10
TEXT_WIDTH = 80

_SINCE_RE = re.compile(r"^(\d+)\s*([smhdw])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

# Rule labels that mean "the rules did not decide": the model was consulted.
_RULE_UNDECIDED = ("undecided", "pass", None)


def parse_since(text: str) -> timedelta:
    """``7d`` -> timedelta(days=7). Raises ValueError on anything else."""
    m = _SINCE_RE.match(text.strip().lower())
    if not m:
        raise ValueError(f"bad --since value {text!r}; use e.g. 7d, 12h, 30m")
    return timedelta(**{_SINCE_UNITS[m.group(2)]: int(m.group(1))})


def parse_ts(value: object) -> datetime | None:
    """Parse a log ``ts`` (ISO 8601, ``Z`` or offset); naive stamps count as UTC."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_decision(row: dict) -> bool:
    """An intake line that sorted a message (not ``new_turn``, ``dedupe`` or store notices)."""
    return row.get("event") == "intake" and isinstance(row.get("label"), str)


def rule_label(row: dict) -> str | None:
    """The rule table's label for the message.

    M1 lines log it as ``rule_label``. M0 lines have only ``rule``: when a rule
    other than ``paste_guard`` fired, the acted ``label`` is the rule's.
    """
    rl = row.get("rule_label")
    if isinstance(rl, str):
        return rl
    if row.get("rule") not in (None, "paste_guard"):
        label = row.get("label")
        return label if isinstance(label, str) else None
    return None


def decided_by(row: dict) -> str:
    """``"rule"`` when the rule table decided alone, else ``"model"``."""
    return "model" if rule_label(row) in _RULE_UNDECIDED else "rule"


def model_label(row: dict) -> str | None:
    """The classifier's label on the same footing as the rules' labels.

    The v2 contract makes ``interrupt`` a flag on ``steer``; the rule table
    names it ``interrupt``. Both mean the same decision.
    """
    clf = row.get("classifier")
    if not isinstance(clf, dict):
        return None
    label = clf.get("label")
    if not isinstance(label, str):
        return None
    if label == "steer" and clf.get("interrupt") is True:
        return "interrupt"
    return label


def is_compared(row: dict) -> bool:
    return decided_by(row) == "rule" and model_label(row) is not None


def is_disagreement(row: dict) -> bool:
    return is_compared(row) and model_label(row) != rule_label(row)


def load_rows(log_dir: Path, since: timedelta | None) -> tuple[list[dict], int]:
    """All intake decision rows under ``log_dir``, newest first, plus the file count.

    With ``since``, rows older than now - since (or without a readable ``ts``)
    are left out.
    """
    cutoff = datetime.now(timezone.utc) - since if since is not None else None
    files = sorted(log_dir.glob("*.jsonl")) if log_dir.is_dir() else []
    rows: list[tuple[datetime | None, int, dict]] = []
    seq = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not is_decision(row):
                continue
            ts = parse_ts(row.get("ts"))
            if cutoff is not None and (ts is None or ts < cutoff):
                continue
            rows.append((ts, seq, row))
            seq += 1
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda item: (item[0] or floor, item[1]), reverse=True)
    return [row for _ts, _seq, row in rows], len(files)


def mean_latency(rows: list[dict]) -> tuple[float | None, int]:
    values = [
        float(row["latency_ms"]) for row in rows
        if isinstance(row.get("latency_ms"), (int, float)) and not isinstance(row.get("latency_ms"), bool)
    ]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def message_text(row: dict) -> str:
    text = row.get("message")
    if not isinstance(text, str) or not text.strip():
        return "(no text logged)"
    text = " ".join(text.split())
    return text if len(text) <= TEXT_WIDTH else text[: TEXT_WIDTH - 1] + "…"


def pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.0f}%" if whole else "n/a"


def report(rows: list[dict], files: int, since_text: str | None) -> str:
    window = f" since {since_text}" if since_text else ""
    if not rows:
        return f"shadow report: no intake decisions found in {files} log file(s){window}\n"
    total = len(rows)
    by_rule = sum(1 for r in rows if decided_by(r) == "rule")
    by_model = total - by_rule
    compared = [r for r in rows if is_compared(r)]
    disagreements = [r for r in rows if is_disagreement(r)]
    mean, calls = mean_latency(rows)

    lines = [
        f"shadow report: {total} intake decision(s) in {files} log file(s){window}",
        f"rule-decided: {by_rule} ({pct(by_rule, total)})",
        f"model-decided: {by_model} ({pct(by_model, total)})",
        f"compared (rule and model both logged): {len(compared)}",
        f"disagreements: {len(disagreements)} of {len(compared)} compared ({pct(len(disagreements), len(compared))})",
    ]
    if mean is None:
        lines.append("mean classifier latency: n/a (no latency_ms logged)")
    else:
        lines.append(f"mean classifier latency: {round(mean)} ms over {calls} call(s)")
    if disagreements:
        lines.append(f"recent disagreements (newest first, up to {RECENT}):")
        for row in disagreements[:RECENT]:
            ts = row.get("ts") if isinstance(row.get("ts"), str) else "?"
            lines.append(f"  {ts}  rule={rule_label(row)}  model={model_label(row)}  {message_text(row)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise rule-vs-model intake decisions from the plugin logs.")
    parser.add_argument("--since", default=None, help="only rows newer than this, e.g. 7d, 12h, 30m")
    args = parser.parse_args(argv)
    since = None
    if args.since is not None:
        try:
            since = parse_since(args.since)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        rows, files = load_rows(paths.data_dir() / "log", since)
        sys.stdout.write(report(rows, files, args.since))
    except Exception as exc:  # never a traceback: this is a reporting tool
        sys.stderr.write(f"shadow_report: {type(exc).__name__}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
