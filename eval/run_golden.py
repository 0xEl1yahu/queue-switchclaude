#!/usr/bin/env python3
"""Golden-set eval runner for the intake classifier (spec section 8, plan Task 8).

Replays every case in ``eval/golden_queue_intake.jsonl`` through the intake
decision path exactly as ``qsc.intake`` would take it (rules first, the
``claude -p`` classifier only on ``undecided``, the 0.9 confidence threshold,
chain dependencies when the model names none, amend targets by the model or by
``qsc.match``) but without a store, then scores each result with
``eval/scoring.py`` and applies the M1 gate.

    python3 eval/run_golden.py [--rules-only] [--cases g01,g05] [--no-cache]
                               [--report eval/reports/<ts>.md] [--claude PATH]

Exit status: 0 when the gate passes, 1 when it fails, 2 on a usage error.
The gate passes when no case scores -1.0 (a missed hard dependency), every
case with ``expected.interrupt`` scores 1.0, and, when the model is in use,
``claude`` was found and no classifier call failed. ``--rules-only`` never
calls ``claude``.

Model answers are cached under ``eval/cache/<id>-<sha1 of prompt+payload>.json``
(``$QSC_EVAL_CACHE`` overrides the directory); ``--no-cache`` ignores existing
entries and refreshes them. The golden set is never written.

Stdlib only. ``eval/`` is outside ``scripts/``, so it puts ``scripts/`` on
``sys.path`` and imports ``qsc``; it tolerates both the M0 (v1) and the M1
(v2) classifier contracts so it can run before and after Tasks 2 to 4 merge.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import inspect
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scoring  # noqa: E402
from qsc import classify, intake, rules, store  # noqa: E402

try:  # Task 3; absent until it merges, in which case amend targets come from the model only.
    from qsc import match
except ImportError:  # pragma: no cover - depends on the merge order
    match = None  # type: ignore[assignment]

GOLDEN_PATH = ROOT / "eval" / "golden_queue_intake.jsonl"
CACHE_DIR = Path(os.environ.get("QSC_EVAL_CACHE") or ROOT / "eval" / "cache")
THRESHOLD = getattr(intake, "CONFIDENCE_THRESHOLD", 0.9)
QUEUED_STATUSES = ("pending", "queued", "shadow_queued")
LIVE_STATUSES = QUEUED_STATUSES + ("running", "parked", "paused")

Classifier = Optional[Callable[[dict], object]]


# --- helpers that tolerate both classifier contracts ------------------------

def _normalise(text: str) -> str:
    norm = getattr(store, "normalise", None)
    if callable(norm):
        return norm(text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split()).lower().rstrip(".,;:!?")


def _field(obj: object, name: str, default):
    value = getattr(obj, name, default)
    return default if value is None else value


def _steer(interrupt: bool = False, note: str | None = None) -> dict:
    out = {"label": "steer", "interrupt": bool(interrupt)}
    if note:
        out["note"] = note
    return out


def _id_number(task_id: str) -> int:
    try:
        return int(str(task_id).lstrip("p"))
    except ValueError:
        return 0


# --- one case, replayed as intake would see it ------------------------------

class _Case:
    """The store-less view of one golden row: known tasks, chain deps, next id."""

    def __init__(self, row: dict) -> None:
        self.row = row
        self.current = row.get("current_task") or None
        self.queue = list(row.get("queue") or [])
        ids = [q["id"] for q in self.queue] + ([self.current["id"]] if self.current else [])
        self._next = max([_id_number(i) for i in ids] + [0]) + 1
        # Tasks created earlier in this case (split parts) extend the chain.
        self._queued_ids = [q["id"] for q in self.queue if q.get("status") in QUEUED_STATUSES]
        self._tasks = self._build_tasks()

    def _build_tasks(self) -> list:
        """store.Task objects for the running task and the live queue, in id order."""
        s = store.Store("eval")
        rows = []
        if self.current:
            rows.append((self.current["id"], self.current.get("intent", ""), "running", []))
        for q in self.queue:
            status = q.get("status", "queued")
            if status in LIVE_STATUSES:
                rows.append((q["id"], q.get("intent", ""), "queued" if status in QUEUED_STATUSES else status, list(q.get("depends_on", []))))
        rows.sort(key=lambda r: _id_number(r[0]))
        tasks = []
        for tid, text, status, deps in rows:
            t = s.new_task(text, deps, None)
            t.id, t.status = tid, status
            tasks.append(t)
        return tasks

    def candidates(self) -> list:
        return list(self._tasks)

    def task(self, task_id: object):
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def next_id(self) -> str:
        tid = f"p{self._next}"
        self._next += 1
        return tid

    def chain_deps(self) -> list[str]:
        """intake._chain_deps: the last queued task, else the running task, else nothing."""
        if self._queued_ids:
            return [self._queued_ids[-1]]
        if self.current:
            return [self.current["id"]]
        return []

    def payload(self, text: str) -> dict:
        queue = [
            {"id": q["id"], "text": q.get("intent", ""),
             "status": "queued" if q.get("status") == "pending" else q.get("status", "queued"),
             "depends_on": list(q.get("depends_on", []))}
            for q in self.queue
        ]
        running = {"id": self.current["id"], "text": self.current.get("intent", "")} if self.current else None
        kwargs = {}
        if match is not None and "candidates" in inspect.signature(classify.payload).parameters:
            kwargs["candidates"] = match.candidates(text, self.candidates())
        return classify.payload(self.row["state"], running, queue, list(self.row.get("recent") or []), text, **kwargs)

    # -- branches --------------------------------------------------------

    def steer(self, d: object) -> dict:
        return self._steer(bool(_field(d, "interrupt", False)), _field(d, "note", None))

    def _steer(self, interrupt: bool, note: str | None) -> dict:
        if note == "pause" and self.current:  # the golden set writes "pause p1"
            note = f"pause {self.current['id']}"
        return _steer(interrupt, note)

    def queue_task(self, deps: list[str] | None, parked: bool) -> dict:
        deps = list(deps) if deps else self.chain_deps()
        tid = self.next_id()
        self._queued_ids.append(tid)
        return {"label": "queue", "id": tid, "depends_on": deps, "parked": bool(parked), "interrupt": False}

    def amend(self, d: object, text: str) -> dict | None:
        """Resolve the target as intake does; None when it cannot (the caller steers)."""
        target = self.task(_field(d, "target", None))
        if target is None and match is not None:
            target = match.pick(text, self.candidates())
        if target is None:
            return None
        out = {"label": "amend", "target": target.id}
        note = _field(d, "note", None)
        new_deps = list(_field(d, "depends_on", []) or [])
        if note == "cancel":
            out["note"] = "cancel"
        elif note == "pause":
            out["note"] = "pause"
        elif new_deps != list(target.depends_on):
            removed = [x for x in target.depends_on if x not in new_deps]
            added = [x for x in new_deps if x not in target.depends_on]
            out["depends_on"] = new_deps
            if removed and not added:
                out["note"] = f"drop dependency on {', '.join(removed)}"
            elif added and not removed:
                out["note"] = f"add dependency on {', '.join(added)}"
            else:
                out["note"] = f"depend on {', '.join(new_deps) or 'nothing'}"
        return out

    def part(self, p: object, text: str) -> dict:
        label = _field(p, "label", "steer")
        if label == "interrupt":
            return _steer(True)
        if label == "queue":
            return self.queue_task(_field(p, "depends_on", []), bool(_field(p, "parked", False)))
        if label == "amend":
            return self.amend(p, text) or _steer()
        return self.steer(p)

    def split(self, d: object) -> dict:
        parts = list(_field(d, "parts", []) or [])
        seen: set[str] = set()
        kept = []
        for p in parts:  # fake-split guard: no empty parts, no two parts about the same thing
            norm = _normalise(str(_field(p, "text", "") or ""))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            kept.append(p)
        if not kept:
            return _steer()
        if len(kept) == 1:
            return self.part(kept[0], str(_field(kept[0], "text", "")))
        if all(_field(p, "label", "steer") == "steer" for p in kept):
            interrupt = any(bool(_field(p, "interrupt", False)) for p in kept)
            notes = [n for n in (_field(p, "note", None) for p in kept) if n]
            return self._steer(interrupt, notes[0] if notes else None)
        return {"label": "split", "parts": [self.part(p, str(_field(p, "text", ""))) for p in kept]}

    def apply(self, d: object, text: str) -> dict:
        label = _field(d, "label", "steer")
        if label == "interrupt":
            return _steer(True)
        if label == "steer":
            return self.steer(d)
        if label == "queue":
            return self.queue_task(_field(d, "depends_on", []), bool(_field(d, "parked", False)))
        if label == "amend":
            return self.amend(d, text) or _steer()
        if label == "split":
            return self.split(d)
        return _steer()


def decide(row: dict, classifier: Classifier) -> dict:
    """The harness decision for one golden row, in the golden ``expected`` shape.

    ``classifier`` maps a payload to a ``classify.Decision`` (or ``None``);
    ``None`` means rules only, so every ``undecided`` message is ``steer``.
    """
    case = _Case(row)
    v = rules.decide(row["message"])
    if v.label == "pass":
        return _steer()
    if v.label == "interrupt":
        return _steer(True)
    if v.label == "queue":
        return case.queue_task(None, bool(getattr(v, "parked", False)))
    if v.label == "control":
        return {"label": "control", "interrupt": False}
    if v.label != "undecided" or classifier is None:
        return _steer()
    d = classifier(case.payload(v.text))
    if d is None:
        return _steer()
    confidence = getattr(d, "confidence", 0.0)
    if not isinstance(confidence, (int, float)) or confidence < THRESHOLD:
        return _steer()
    return case.apply(d, v.text)


# --- the cached model classifier ---------------------------------------------

def cache_path(case_id: str, prompt: str, payload: dict, cache_dir: Path = CACHE_DIR) -> Path:
    digest = hashlib.sha1((prompt + "\n" + json.dumps(payload, sort_keys=True)).encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{case_id}-{digest}.json"


def classify_cached(case_id: str, payload: dict, claude: str = "claude", use_cache: bool = True,
                    cache_dir: Path = CACHE_DIR, stats: dict | None = None):
    """``classify.run`` with a per-case file cache. Failures are never cached."""
    stats = stats if stats is not None else {}
    stats.update(called=True, cached=False, error=False, latency_ms=None)
    prompt = classify._system_prompt() or ""
    path = cache_path(case_id, prompt, payload, cache_dir)
    if use_cache:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            d = classify._validate(cached.get("decision"))
            if d is not None:
                stats["cached"] = True
                return d
        except (OSError, ValueError, AttributeError):
            pass
    t0 = time.perf_counter()
    d = classify.run(payload, claude=claude)
    stats["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    if d is None:
        stats["error"] = True
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"case": case_id, "payload": payload, "decision": dataclasses.asdict(d)}, indent=2), encoding="utf-8")
    except OSError:
        pass
    return d


# --- the run ------------------------------------------------------------------

def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_case(row: dict, rules_only: bool, claude: str, use_cache: bool, cache_dir: Path) -> dict:
    v = rules.decide(row["message"])
    stats: dict = {"called": False, "cached": False, "error": False, "latency_ms": None}
    if rules_only:
        classifier = None
    else:
        def classifier(payload: dict):
            return classify_cached(row["id"], payload, claude=claude, use_cache=use_cache, cache_dir=cache_dir, stats=stats)
    got = decide(row, classifier)
    score, reason = scoring.score_case(row, got)
    if v.label != "undecided":
        by = f"rule:{v.rule}"
    elif not stats["called"]:
        by = "none"
    elif stats["error"]:
        by = "model:error"
    elif stats["cached"]:
        by = "model:cache"
    else:
        by = "model"
    return {
        "id": row["id"], "context": row.get("context", ""), "difficulty": row.get("difficulty", ""),
        "expected": row["expected"]["label"], "got": got.get("label"), "score": score, "reason": reason,
        "by": by, "rule_decided": v.label != "undecided", "model_error": stats["error"],
        "latency_ms": stats["latency_ms"], "interrupt_case": bool(row["expected"].get("interrupt")),
        "decision": got,
    }


def summarise(results: list[dict], rules_only: bool, claude_found: bool) -> dict:
    def bucket(key):
        out = {}
        for r in results:
            b = out.setdefault(r[key], {"cases": 0, "passed": 0, "score": 0.0})
            b["cases"] += 1
            b["passed"] += r["score"] == 1.0
            b["score"] += r["score"]
        return out

    interrupt_cases = [r for r in results if r["interrupt_case"]]
    missed = sum(r["score"] == -1.0 for r in results)
    errors = sum(r["model_error"] for r in results)
    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    gate_reasons = []
    if missed:
        gate_reasons.append(f"{missed} missed hard dependenc{'y' if missed == 1 else 'ies'}")
    bad_interrupts = [r["id"] for r in interrupt_cases if r["score"] != 1.0]
    if bad_interrupts:
        gate_reasons.append(f"interrupt case(s) wrong: {', '.join(bad_interrupts)}")
    if not rules_only and not claude_found:
        gate_reasons.append("claude not found on PATH, so the model never ran")
    if errors:
        gate_reasons.append(f"{errors} classifier call(s) failed (fail-open steer)")
    return {
        "cases": len(results),
        "passed": sum(r["score"] == 1.0 for r in results),
        "total_score": sum(r["score"] for r in results),
        "by_label": bucket("expected"),
        "by_difficulty": bucket("difficulty"),
        "missed_hard_dependency": missed,
        "interrupt_total": len(interrupt_cases),
        "interrupt_correct": len(interrupt_cases) - len(bad_interrupts),
        "rule_decided": sum(r["rule_decided"] for r in results),
        "model_errors": errors,
        "mean_latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
        "gate_pass": not gate_reasons,
        "gate_reasons": gate_reasons,
    }


def _table_rows(results: list[dict]) -> list[list[str]]:
    return [[r["id"], r["context"], r["difficulty"], r["expected"], str(r["got"]), f"{r['score']:+.1f}", r["reason"], r["by"]] for r in results]


HEADERS = ["id", "context", "difficulty", "expected", "got", "score", "reason", "by"]


def render_text(results: list[dict], summary: dict) -> str:
    rows = _table_rows(results)
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(HEADERS)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(HEADERS))]
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    lines.append("")
    lines.extend(_summary_lines(summary))
    return "\n".join(lines) + "\n"


def _summary_lines(s: dict) -> list[str]:
    lines = [f"cases: {s['cases']}  passed: {s['passed']}  total score: {s['total_score']:+.1f}"]
    for name, key in (("by label", "by_label"), ("by difficulty", "by_difficulty")):
        parts = [f"{k} {b['passed']}/{b['cases']} ({b['score']:+.1f})" for k, b in s[key].items()]
        lines.append(f"{name}: " + ", ".join(parts))
    lines.append(f"missed_hard_dependency: {s['missed_hard_dependency']}")
    lines.append(f"interrupt cases correct: {s['interrupt_correct']}/{s['interrupt_total']}")
    share = (100.0 * s["rule_decided"] / s["cases"]) if s["cases"] else 0.0
    lines.append(f"decided by rules alone: {s['rule_decided']}/{s['cases']} ({share:.0f}%)")
    lines.append(f"classifier errors: {s['model_errors']}")
    if s["mean_latency_ms"] is not None:
        lines.append(f"mean classifier latency: {s['mean_latency_ms']} ms (uncached calls)")
    lines.append("gate: PASS" if s["gate_pass"] else "gate: FAIL (" + "; ".join(s["gate_reasons"]) + ")")
    return lines


def render_markdown(results: list[dict], summary: dict, command: str) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    lines = ["# Golden-set eval", "", f"Run at {ts} with `{command}`.", "",
             "| " + " | ".join(HEADERS) + " |", "|" + "---|" * len(HEADERS)]
    for r in _table_rows(results):
        lines.append("| " + " | ".join(r) + " |")
    lines.append("")
    lines.extend(f"- {line}" for line in _summary_lines(summary))
    lines.append("")
    failing = [r for r in results if r["score"] != 1.0]
    if failing:
        lines.append("## Failing cases")
        lines.append("")
        for r in failing:
            lines.append(f"- {r['id']} ({r['reason']}, {r['score']:+.1f}): got `{json.dumps(r['decision'], sort_keys=True)}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the golden intake set through the harness decision path and score it.")
    ap.add_argument("--rules-only", action="store_true", help="never call claude; undecided messages become steer")
    ap.add_argument("--cases", help="comma-separated case ids, e.g. g01,g05")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached model answers (fresh answers are still written)")
    ap.add_argument("--report", help="write a markdown report to this path")
    ap.add_argument("--claude", default="claude", help="the claude binary to call (default: claude on PATH)")
    ap.add_argument("--golden", default=str(GOLDEN_PATH), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    rows = load_golden(Path(args.golden))
    if args.cases:
        wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
        known = {r["id"] for r in rows}
        unknown = [c for c in wanted if c not in known]
        if unknown:
            print(f"run_golden: unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        rows = [r for r in rows if r["id"] in wanted]

    claude_found = True
    rules_only = args.rules_only
    if not rules_only:
        claude_found = shutil.which(args.claude) is not None or Path(args.claude).is_file()
        if not claude_found:
            print(f"run_golden: '{args.claude}' not found; running the rules only (the gate fails)", file=sys.stderr)
            rules_only = True

    results = [run_case(r, rules_only, args.claude, not args.no_cache, CACHE_DIR) for r in rows]
    summary = summarise(results, args.rules_only, claude_found)
    sys.stdout.write(render_text(results, summary))

    if args.report:
        command = "python3 eval/run_golden.py " + " ".join(argv if argv is not None else sys.argv[1:])
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(results, summary, command.strip()), encoding="utf-8")
    return 0 if summary["gate_pass"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("run_golden: interrupted", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # never a traceback (plan review focus 5)
        print(f"run_golden: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
