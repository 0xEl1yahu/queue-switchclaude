"""Background comparison: ask the classifier about a message the rules decided.

``python3 compare.py <session_id> <json-file>`` is spawned detached by intake
when the plugin is active and the ``shadow_compare`` option is on, so the
rule path keeps its ~90 ms and the model's answer still lands in the log,
as an ``shadow_compare`` event beside the ``intake`` line that acted. The
JSON file holds ``payload`` (what the classifier sees), ``message``,
``rule``, ``rule_label`` and ``prompt_id``; it is deleted once read.

Exits 0 with no output whatever happens: this process answers to nobody.
"""
import time; _T0 = time.monotonic()
import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(argv: list[str]) -> None:
    from qsc import classify, log

    session_id, path = argv[0], argv[1]
    with open(path, "r", encoding="utf-8") as fh:
        job = json.load(fh)
    try:
        os.unlink(path)
    except OSError:
        pass
    started = time.monotonic()
    decision = classify.run(job["payload"])
    latency_ms = int(round((time.monotonic() - started) * 1000))
    log.record(
        session_id, "shadow_compare",
        message=job.get("message"),
        rule=job.get("rule"),
        rule_label=job.get("rule_label"),
        classifier=dataclasses.asdict(decision) if decision is not None else None,
        latency_ms=latency_ms,
        prompt_id=job.get("prompt_id"),
        hook_ms=int(round((time.monotonic() - _T0) * 1000)),
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 3:
            main(sys.argv[1:])
    except BaseException:
        pass
    sys.exit(0)
