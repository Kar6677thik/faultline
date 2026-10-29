import json

from faultline.groups import Group, failure_signatures, matches, summarize

TARGET = "a" * 64
OTHER = "b" * 64


def report(verdict, signatures=(), lifecycle="completed", run_id="r"):
    findings = [
        {
            "invariant_id": "orders.at_most_one_debit",
            "version": 1,
            "status": "FAIL",
            "logical_id": "purchase-a",
            "failures": [{"code": f"code-{i}"} for i, _ in enumerate(signatures)],
            "signatures": list(signatures),
        }
    ]
    return {
        "run_id": run_id,
        "verdict": verdict,
        "lifecycle": lifecycle,
        "invariants": findings if signatures else [],
        "cleanup": {"complete": True},
    }


def test_controlled_mixture_has_exact_counts_and_both_denominators():
    reports = [
        report("FAIL", [TARGET]),
        report("FAIL", [TARGET]),
        report("FAIL", [OTHER]),
        report("PASS"),
        report("INCONCLUSIVE"),
        # A harness error that also observed the violation is not a reproduction.
        report("ERROR", [TARGET]),
        # An interrupted run is never a conclusive reproduction, even with the target.
        report("FAIL", [TARGET], lifecycle="interrupted"),
    ]
    summary = summarize(reports, 10, TARGET)
    assert summary["counts"] == {"PASS": 1, "FAIL": 4, "INCONCLUSIVE": 1, "ERROR": 1}
    assert summary["scheduled"] == 10
    assert summary["started"] == 7
    assert summary["completed"] == 6
    assert summary["interrupted"] == 1
    assert summary["unstarted"] == 3
    assert summary["matching_failures"] == 2
    assert summary["other_failures"] == 2
    assert summary["reproduction"]["per_started"]["ratio"] == "2/7"
    # Conclusive: completed PASS or FAIL only (two matching, one other, one pass).
    assert summary["reproduction"]["per_conclusive"]["ratio"] == "2/4"
    assert summary["verdict"] == "ERROR"


def test_zero_denominators_are_not_available():
    summary = summarize([report("ERROR"), report("INCONCLUSIVE")], 5, TARGET)
    assert summary["reproduction"]["per_conclusive"] == {
        "numerator": 0,
        "denominator": 0,
        "ratio": "not_available",
    }
    empty = summarize([], 5, TARGET)
    assert empty["reproduction"]["per_started"]["ratio"] == "not_available"
    assert empty["verdict"] is None


def test_without_target_no_reproduction_is_claimed():
    summary = summarize([report("FAIL", [TARGET])], 1, None)
    assert summary["matching_failures"] is None
    assert "reproduction" not in summary
    assert not matches(report("FAIL", [TARGET]), None)


def test_signature_catalog_keeps_readable_fields():
    catalog = failure_signatures(report("FAIL", [TARGET, OTHER]))
    assert catalog[TARGET] == {
        "invariant_id": "orders.at_most_one_debit",
        "version": 1,
        "code": "code-0",
        "logical_id": "purchase-a",
    }
    assert set(catalog) == {TARGET, OTHER}


def test_group_record_is_persisted_after_each_trial(tmp_path):
    group = Group(tmp_path, "replay", scheduled=3, signature=TARGET, details={"variant": "x"})
    saved = json.loads(group.path.read_text())
    assert saved["status"] == "running" and saved["started"] == 0
    group.add(report("FAIL", [TARGET], run_id="one"))
    saved = json.loads(group.path.read_text())
    assert saved["run_ids"] == ["one"]
    assert saved["trials"][0]["matches_target"] is True
    summary = group.close("interrupted")
    saved = json.loads(group.path.read_text())
    assert saved["status"] == "interrupted"
    assert saved["unstarted"] == summary["unstarted"] == 2
