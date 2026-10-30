import inspect
import json
from pathlib import Path

import pytest

from faultline import calibration, orders, orders_b
from faultline.adapter_loader import resolve_adapter
from faultline.adapters import ORDER_VARIANTS
from faultline.calibration import load_corpus, run_corpus, summarize_pair, target_found
from faultline.models import load_scenario

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples/orders/corpus.json"
TARGET = {
    "invariant_id": "orders.at_most_one_debit",
    "code": "duplicate_debit",
    "logical_id": "purchase-a",
}


def report(verdict, code="duplicate_debit", lifecycle="completed", logical_id="purchase-a"):
    failing = verdict in {"FAIL", "ERROR"}
    return {
        "verdict": verdict,
        "lifecycle": lifecycle,
        "invariants": [
            {
                "invariant_id": "orders.at_most_one_debit",
                "version": 1,
                "logical_id": logical_id,
                "failures": [{"code": code}] if failing else [],
                "signatures": ["a" * 64] if failing else [],
            }
        ],
    }


def test_target_requires_completed_fail_with_exact_fields():
    assert target_found(report("FAIL"), TARGET)
    assert not target_found(report("ERROR"), TARGET)  # a harness error never detects
    assert not target_found(report("FAIL", lifecycle="interrupted"), TARGET)
    assert not target_found(report("FAIL", code="wrong_amount"), TARGET)
    assert not target_found(report("FAIL", logical_id="purchase-b"), TARGET)


def test_pair_criteria_lenient_strict_and_correct():
    trials = [{"verdict": "FAIL", "detected": True}, {"verdict": "PASS", "detected": False}]
    assert summarize_pair(trials, TARGET)["met"]  # AT-021: at least once
    assert not summarize_pair(trials, TARGET, strict=True)["met"]  # CI: every trial
    assert summarize_pair([{"verdict": "PASS"}] * 3, None)["met"]
    assert not summarize_pair([{"verdict": "PASS"}, {"verdict": "INCONCLUSIVE"}], None)["met"]


def test_corpus_declares_valid_scenarios_for_registered_variants():
    corpus = load_corpus(CORPUS)
    defects = {d["defect"] for d in corpus["defects"]}
    assert len(defects) == len(corpus["defects"]) == 6  # distinct types, not repeats
    pairs = [(d["variant"], d["scenario"]) for d in corpus["defects"]] + [
        (variant, scenario)
        for scenario in corpus["correct"]["scenarios"]
        for variant in corpus["correct"]["variants"]
    ]
    for variant, scenario in pairs:
        assert variant in ORDER_VARIANTS
        resolve_adapter(None, load_scenario(CORPUS.parent / scenario), variant)


def test_bad_corpus_targets_are_rejected(tmp_path):
    corpus = json.loads(CORPUS.read_text())
    corpus["defects"][0]["target"] = {"invariant_id": "x"}
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus))
    with pytest.raises(ValueError, match="target"):
        load_corpus(path)


def test_application_and_adapter_variant_lists_agree():
    assert set(ORDER_VARIANTS) == set(orders.VARIANTS)


def test_correct_b_is_not_correct_a_with_a_flag():
    source = inspect.getsource(orders_b)
    # B relies on serializable isolation, not row locks or A's code path.
    assert "IsolationLevel.SERIALIZABLE" in source
    assert "FOR UPDATE" not in source
    assert "transact_order" not in source and "_identity_checks" not in source
    # Only correct-A gets unique indexes; B must not depend on them.
    prepare = inspect.getsource(orders.prepare)
    assert 'if variant == "correct":' in prepare


def test_every_trial_is_recorded_and_summarized(monkeypatch, tmp_path):
    corpus = json.loads(CORPUS.read_text())
    corpus["defects"] = corpus["defects"][:1]
    corpus["correct"]["scenarios"] = ["control.yaml"]
    path = CORPUS.parent / "corpus.json"
    copy = tmp_path / "corpus.json"
    copy.write_text(json.dumps(corpus))
    outcomes = iter(
        [
            {"verdict": "FAIL", "lifecycle": "completed", "report": report("FAIL")},
            {"verdict": "ERROR", "lifecycle": "completed", "report": report("ERROR")},
            {"verdict": "PASS", "lifecycle": "completed", "report": report("PASS")},
            {"verdict": "PASS", "lifecycle": "completed", "report": report("PASS")},
            {"verdict": "PASS", "lifecycle": "completed", "report": report("PASS")},
            {"verdict": "INCONCLUSIVE", "lifecycle": "completed", "report": report("INCONCLUSIVE")},
        ]
    )
    calls = []

    def fake(scenario, variant, output, timeout, project=None):
        calls.append((Path(scenario).name, variant))
        return dict(next(outcomes))

    monkeypatch.setattr(calibration, "one_trial", fake)
    # Scenario paths resolve beside the corpus file, so keep the real examples directory.
    monkeypatch.setattr(calibration, "load_corpus", lambda _p: corpus)
    record = run_corpus(path, tmp_path / "out", 2, "all", 60)
    defect, correct_a, correct_b = record["pairs"]
    assert defect["detections"] == 1 and defect["counts"]["ERROR"] == 1 and defect["met"]
    assert correct_a["met"] and not correct_b["met"]  # one INCONCLUSIVE blocks the gate
    assert not record["met"]
    assert len(calls) == 6
    table = (tmp_path / "out" / "calibration.md").read_text()
    assert (
        "| missing_idempotency | `unsafe` | `lost-response.yaml` | 0 | 1 | 0 | 1 | 1/2 | yes |"
        in table
    )


def test_transfer_corpus_pairs_validate_through_the_project():
    from faultline.projects import load_project

    path = ROOT / "examples/transfers/corpus.json"
    corpus = load_corpus(path)
    loaded = load_project(path.parent / corpus["project"])
    pairs = [(d["variant"], d["scenario"]) for d in corpus["defects"]] + [
        (variant, scenario)
        for scenario in corpus["correct"]["scenarios"]
        for variant in corpus["correct"]["variants"]
    ]
    for variant, scenario in pairs:
        resolve_adapter(loaded.project, loaded.load_scenario(scenario), variant)
    # The instrumented checkpoint is excluded for the atomic variant, not silently passed.
    assert "checkpoint-restart.yaml" not in corpus["correct"]["scenarios"]


def test_probabilistic_defects_need_one_detection_and_no_error_in_strict_mode():
    some = [{"verdict": "PASS", "detected": False}, {"verdict": "FAIL", "detected": True}]
    assert summarize_pair(some, TARGET, strict=True, probabilistic=True)["met"]
    assert not summarize_pair(some, TARGET, strict=True)["met"]
    errored = [*some, {"verdict": "ERROR", "detected": False}]
    assert not summarize_pair(errored, TARGET, strict=True, probabilistic=True)["met"]
    missed = [{"verdict": "PASS", "detected": False}] * 3
    assert not summarize_pair(missed, TARGET, strict=True, probabilistic=True)["met"]


def test_strict_mode_gives_probabilistic_defects_their_minimum_trials(monkeypatch, tmp_path):
    corpus = json.loads(CORPUS.read_text())
    corpus["defects"] = [d for d in corpus["defects"] if d["variant"] == "non_atomic"]
    assert corpus["defects"][0]["detection"] == "probabilistic"
    calls = []

    def fake(scenario, variant, output, timeout, project=None):
        calls.append(variant)
        return {"verdict": "PASS", "lifecycle": "completed", "report": report("PASS")}

    monkeypatch.setattr(calibration, "one_trial", fake)
    monkeypatch.setattr(calibration, "load_corpus", lambda _p: corpus)
    record = run_corpus(CORPUS, tmp_path / "out", 1, "defects", 60, strict=True)
    assert len(calls) == corpus["defects"][0]["min_trials"]
    assert not record["met"]  # zero detections is never acceptable
