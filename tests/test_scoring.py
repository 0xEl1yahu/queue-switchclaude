"""Task 8: asymmetric scoring of one harness decision against a golden case."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import scoring  # noqa: E402

G = {json.loads(l)["id"]: json.loads(l) for l in open(ROOT / "eval" / "golden_queue_intake.jsonl")}


def test_scoring_exact_and_also_acceptable():
    assert scoring.score_case(G["g04"], {"label": "steer"}) == (1.0, "ok")
    assert scoring.score_case(G["g04"], {"label": "queue", "id": "p2", "depends_on": ["p1"]}) == (1.0, "ok")


def test_scoring_missed_hard_dependency_is_negative():
    assert scoring.score_case(G["g05"], {"label": "queue", "id": "p2", "depends_on": []}) == (-1.0, "missed_hard_dependency")


def test_scoring_extra_dependency_is_zero_not_negative():
    assert scoring.score_case(G["g08"], {"label": "queue", "id": "p2", "depends_on": ["p1"]})[0] == 0.0


def test_scoring_split_parts_in_order():
    got = {"label": "split", "parts": [{"label": "steer"}, {"label": "queue", "id": "p2", "depends_on": ["p1"]}]}
    assert scoring.score_case(G["g09"], got) == (1.0, "ok")


def test_scoring_interrupt_flag():
    assert scoring.score_case(G["g30"], {"label": "steer", "interrupt": True}) == (1.0, "ok")
    assert scoring.score_case(G["g30"], {"label": "steer"})[0] == 0.0


# --- additional tests beyond the plan ---------------------------------------

def test_outcome_label_mismatch_reason():
    assert scoring.outcome({"label": "queue", "depends_on": []}, {"label": "steer"}) == (0.0, "label")
    assert scoring.outcome({"label": "steer"}, {"label": "queue", "depends_on": []}) == (0.0, "label")


def test_outcome_extra_dependency_reason_and_confidence_ignored():
    assert scoring.outcome({"label": "queue", "id": "p2", "depends_on": []},
                           {"label": "queue", "id": "p2", "depends_on": ["p1"], "confidence": 0.5}) == (0.0, "dependency_extra")
    assert scoring.outcome({"label": "steer"}, {"label": "steer", "confidence": 0.1}) == (1.0, "ok")


def test_outcome_parked_flag():
    assert scoring.score_case(G["g33"], {"label": "queue", "id": "p2", "depends_on": ["p1"], "parked": True}) == (1.0, "ok")
    assert scoring.score_case(G["g33"], {"label": "queue", "id": "p2", "depends_on": ["p1"]}) == (0.0, "parked")
    assert scoring.score_case(G["g35"], {"label": "queue", "id": "p2", "depends_on": ["p1"], "parked": True}) == (0.0, "parked")


def test_outcome_amend_target_and_note():
    assert scoring.score_case(G["g16"], {"label": "amend", "target": "p3", "note": "cancel"}) == (1.0, "ok")
    assert scoring.score_case(G["g16"], {"label": "amend", "target": "p3"}) == (0.0, "note")
    assert scoring.score_case(G["g16"], {"label": "amend", "target": "p2", "note": "cancel"}) == (0.0, "target")
    assert scoring.score_case(G["g23"], {"label": "amend", "target": "p2"}) == (1.0, "ok")


def test_outcome_steer_note():
    assert scoring.score_case(G["g44"], {"label": "steer", "note": "approve"}) == (1.0, "ok")
    assert scoring.score_case(G["g44"], {"label": "steer"}) == (0.0, "note")
    assert scoring.score_case(G["g45"], {"label": "steer", "note": "approve"}) == (0.0, "note")


def test_split_part_count_and_part_dependency_miss():
    assert scoring.score_case(G["g09"], {"label": "split", "parts": [{"label": "steer"}]}) == (0.0, "parts")
    got = {"label": "split", "parts": [{"label": "steer"}, {"label": "queue", "id": "p2", "depends_on": []}]}
    assert scoring.score_case(G["g09"], got) == (-1.0, "missed_hard_dependency")
    got = {"label": "split", "parts": [{"label": "queue", "id": "p2", "depends_on": ["p1"]}, {"label": "steer"}]}
    assert scoring.score_case(G["g09"], got) == (0.0, "parts")


def test_split_g20_needs_both_endpoint_dependencies():
    ok = {"label": "split", "parts": [
        {"label": "queue", "id": "p2", "depends_on": ["p1"]},
        {"label": "queue", "id": "p3", "depends_on": ["p2"]},
        {"label": "queue", "id": "p4", "depends_on": ["p2", "p3"]}]}
    assert scoring.score_case(G["g20"], ok) == (1.0, "ok")
    chain = {"label": "split", "parts": [
        {"label": "queue", "id": "p2", "depends_on": ["p1"]},
        {"label": "queue", "id": "p3", "depends_on": ["p2"]},
        {"label": "queue", "id": "p4", "depends_on": ["p3"]}]}
    assert scoring.score_case(G["g20"], chain) == (-1.0, "missed_hard_dependency")


def test_score_case_prefers_best_alternative():
    # g25: expected [] and also_acceptable ["p1"]; a chain dependency is a full pass, a miss is not possible.
    assert scoring.score_case(G["g25"], {"label": "queue", "id": "p3", "depends_on": ["p1"]}) == (1.0, "ok")
    assert scoring.score_case(G["g25"], {"label": "queue", "id": "p3", "depends_on": ["p2"]})[0] == 0.0


def test_outcome_tolerates_non_dict_and_missing_keys():
    assert scoring.outcome({"label": "steer"}, None) == (0.0, "label")
    assert scoring.outcome({"label": "queue", "depends_on": ["p1"]}, {"label": "queue"}) == (-1.0, "missed_hard_dependency")
    assert scoring.outcome({"label": "split", "parts": [{"label": "steer"}]}, {"label": "split"}) == (0.0, "parts")
