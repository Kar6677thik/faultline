import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from faultline.artifacts import Bundle, read_bundle
from faultline.models import load_scenario
from faultline.server import ASSETS, create_app, presentable
from faultline.verifier import EvidenceError, UnsupportedArtifact

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "k" * 43
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BIG = 2**63 - 7


def completed(output, verdict="FAIL", created_at=None):
    scenario = load_scenario(ROOT / "examples/orders/lost-response.yaml")
    bundle = Bundle(output, scenario, "unsafe")
    if created_at:
        bundle.manifest["created_at"] = created_at
    snapshot = {
        "snapshot_id": "final",
        "complete": True,
        "quiescent": True,
        "content_hash": "c" * 64,
        "records": {
            "accounts": [{"account_id": "buyer-a", "balance_minor": BIG}],
            "ledger": [{"entry_id": f"debit-{i}", "amount_minor": 200000} for i in range(3)],
        },
    }
    bundle.write("snapshots/final.json", snapshot)
    bundle.event("attempt", {"started_ns": BIG}, attempt_id="purchase_a/1")
    bundle.event("client_outcome", {"outcome": "timeout"}, attempt_id="purchase_a/1")
    bundle.finalize(
        {
            "verdict": verdict,
            "lifecycle": "completed",
            "invariants": [],
            "diagnostics": [],
            "attempts": [{"attempt_id": "purchase_a/1", "outcome": "timeout"}],
            "coverage": {"required_applied": 1, "required_total": 1},
            "cleanup": {"complete": True},
        }
    )
    return bundle


def client(output, groups=True):
    app = create_app(output / "runs", output / "groups" if groups else None, TOKEN, {"port": PORT})
    return TestClient(app, base_url=BASE)


def reseal(bundle_path: Path):
    """Rewrite inventory hashes so semantic checks, not hashes, must catch the defect."""
    manifest = json.loads((bundle_path / "manifest.json").read_text())
    for name in manifest["artifacts"]:
        raw = (bundle_path / name).read_bytes()
        manifest["artifacts"][name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
    (bundle_path / "manifest.json").write_text(json.dumps(manifest))


# --- local web boundary (AT-020) --------------------------------------------------


def test_token_is_required_for_the_api_but_not_for_health(tmp_path):
    api = client(tmp_path)
    assert api.get("/health").json()["data"]["status"] == "ok"
    assert api.get("/api/v1/runs").status_code == 401
    wrong = api.get("/api/v1/runs", headers={"Authorization": "Bearer " + "x" * 43})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"
    assert api.get("/api/v1/runs", headers=AUTH).status_code == 200


def test_host_and_origin_are_checked(tmp_path):
    api = client(tmp_path)
    rebinding = api.get("/api/v1/runs", headers={**AUTH, "Host": "evil.example:8765"})
    assert rebinding.status_code == 403
    foreign = api.get("/api/v1/runs", headers={**AUTH, "Origin": "http://evil.example"})
    assert foreign.status_code == 403
    same = api.get("/api/v1/runs", headers={**AUTH, "Origin": BASE})
    assert same.status_code == 200
    assert "access-control-allow-origin" not in same.headers


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_mutation_methods_are_rejected(tmp_path, method):
    response = client(tmp_path).request(method, "/api/v1/runs", headers=AUTH)
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers


def test_viewer_page_has_strict_headers_and_no_data_or_third_party_assets(tmp_path):
    if not (ASSETS / "index.html").is_file():
        pytest.skip("viewer assets are not built")
    completed(tmp_path)
    response = client(tmp_path).get("/runs/whatever")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    page = response.text
    assert "buyer-a" not in page and "http://" not in page and "https://" not in page
    for asset in (ASSETS / "assets").iterdir():
        text = asset.read_text(encoding="utf-8")
        # No network loads from other origins (React's error-help URLs are inert strings).
        assert '<script src="http' not in text and "@import url(http" not in text


def test_artifacts_are_addressed_by_opaque_id_only(tmp_path):
    bundle = completed(tmp_path)
    api = client(tmp_path)
    detail = api.get(f"/api/v1/runs/{bundle.run_id}", headers=AUTH).json()["data"]
    assert "execution_root" not in detail["manifest"]
    report_md = next(a for a in detail["artifacts"] if a["name"] == "report.md")
    response = api.get(
        f"/api/v1/runs/{bundle.run_id}/artifacts/{report_md['artifact_id']}", headers=AUTH
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.content == (bundle.path / "report.md").read_bytes()
    for attempt in ("..%2Fmanifest.json", "report.md", "../../etc/passwd"):
        escaped = api.get(f"/api/v1/runs/{bundle.run_id}/artifacts/{attempt}", headers=AUTH)
        assert escaped.status_code == 404
    for run in ("..", bundle.run_id.upper(), "not-a-uuid"):
        assert api.get(f"/api/v1/runs/{run}", headers=AUTH).status_code == 404


def test_artifact_changed_after_listing_is_refused(tmp_path):
    bundle = completed(tmp_path)
    api = client(tmp_path)
    detail = api.get(f"/api/v1/runs/{bundle.run_id}", headers=AUTH).json()["data"]
    plan = next(a for a in detail["artifacts"] if a["name"] == "plan.json")
    (bundle.path / "plan.json").write_text("{}")
    response = api.get(
        f"/api/v1/runs/{bundle.run_id}/artifacts/{plan['artifact_id']}", headers=AUTH
    )
    assert response.status_code == 422


# --- API contract -----------------------------------------------------------------


def test_unsafe_integers_money_and_nanoseconds_become_strings(tmp_path):
    assert presentable({"count": 3, "amount_minor": 5, "x_ns": 7, "big": BIG, "flag": True}) == {
        "count": 3,
        "amount_minor": "5",
        "x_ns": "7",
        "big": str(BIG),
        "flag": True,
    }
    bundle = completed(tmp_path)
    api = client(tmp_path)
    rows = api.get(
        f"/api/v1/runs/{bundle.run_id}/snapshots/final?record_set=accounts", headers=AUTH
    ).json()["data"]["rows"]
    assert rows == [{"account_id": "buyer-a", "balance_minor": str(BIG)}]
    events = api.get(f"/api/v1/runs/{bundle.run_id}/events", headers=AUTH).json()["data"]
    assert events["events"][0]["payload"]["started_ns"] == str(BIG)
    assert isinstance(events["events"][0]["sequence"], int)


def test_run_listing_is_stable_across_pages_and_filters(tmp_path):
    first = completed(tmp_path, "PASS", "2026-10-29T10:00:00+00:00")
    second = completed(tmp_path, "FAIL", "2026-10-29T11:00:00+00:00")
    third = completed(tmp_path, "FAIL", "2026-10-29T12:00:00+00:00")
    api = client(tmp_path)
    page = api.get("/api/v1/runs?limit=2", headers=AUTH).json()
    assert [r["run_id"] for r in page["data"]] == [third.run_id, second.run_id]
    # A run created after the first page must not shift the next page.
    completed(tmp_path, "FAIL", "2099-01-01T00:00:00+00:00")
    cursor = page["meta"]["next_cursor"]
    following = api.get(f"/api/v1/runs?limit=2&cursor={cursor}", headers=AUTH).json()
    assert [r["run_id"] for r in following["data"]] == [first.run_id]
    assert following["meta"]["next_cursor"] is None
    failing = api.get("/api/v1/runs?verdict=FAIL", headers=AUTH).json()["data"]
    assert {r["verdict"] for r in failing} == {"FAIL"}
    for query in ("limit=0", "limit=201", "verdict=GREEN", "cursor=%%%"):
        assert api.get(f"/api/v1/runs?{query}", headers=AUTH).status_code == 400


def test_incomplete_runs_are_running_or_interrupted_never_completed(tmp_path):
    scenario = load_scenario(ROOT / "examples/orders/lost-response.yaml")
    live = Bundle(tmp_path, scenario, "unsafe")
    live.event("lifecycle", {"stage": "executing"})
    with (live.path / "history.ndjson").open("a") as stream:
        stream.write('{"sequence": 2, "partial')
    stale = Bundle(tmp_path, scenario, "unsafe")
    old = time.time() - 3600
    for path in stale.path.rglob("*"):
        os.utime(path, (old, old))
    api = client(tmp_path)
    detail = api.get(f"/api/v1/runs/{live.run_id}", headers=AUTH).json()["data"]
    assert detail["verdict"] == "RUNNING" and detail["completeness"] == "incomplete"
    assert detail["report"] is None
    events = api.get(f"/api/v1/runs/{live.run_id}/events", headers=AUTH).json()
    assert [e["sequence"] for e in events["data"]["events"]] == [1]
    assert "Incomplete trailing history line excluded" in events["meta"]["diagnostics"]
    stale_detail = api.get(f"/api/v1/runs/{stale.run_id}", headers=AUTH).json()["data"]
    assert stale_detail["verdict"] == "INTERRUPTED"
    missing = api.get(f"/api/v1/runs/{live.run_id}/snapshots/final?record_set=x", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "artifact_not_ready"


def test_event_pagination_and_etag(tmp_path):
    bundle = completed(tmp_path)
    api = client(tmp_path)
    page = api.get(f"/api/v1/runs/{bundle.run_id}/events?limit=1", headers=AUTH).json()["data"]
    assert page["has_more"] and page["next_after_sequence"] == 1
    rest = api.get(
        f"/api/v1/runs/{bundle.run_id}/events?after_sequence=1&limit=1000", headers=AUTH
    ).json()["data"]
    assert rest["events"][0]["sequence"] == 2 and not rest["has_more"]
    assert (
        api.get(f"/api/v1/runs/{bundle.run_id}/events?limit=1001", headers=AUTH).status_code == 400
    )
    first = api.get(f"/api/v1/runs/{bundle.run_id}", headers=AUTH)
    again = api.get(
        f"/api/v1/runs/{bundle.run_id}", headers={**AUTH, "If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304


def test_snapshot_record_sets_and_bounds(tmp_path):
    bundle = completed(tmp_path)
    api = client(tmp_path)
    base = f"/api/v1/runs/{bundle.run_id}/snapshots/final"
    page = api.get(f"{base}?record_set=ledger&offset=1&limit=1", headers=AUTH).json()["data"]
    assert page["total"] == 3 and [r["entry_id"] for r in page["rows"]] == ["debit-1"]
    assert api.get(f"{base}?record_set=secrets", headers=AUTH).status_code == 404
    assert api.get(f"{base}?record_set=ledger&limit=1001", headers=AUTH).status_code == 400
    assert (
        api.get(
            f"/api/v1/runs/{bundle.run_id}/snapshots/%2E%2E?record_set=ledger", headers=AUTH
        ).status_code
        == 404
    )


def test_corrupt_and_unsupported_bundles_have_distinct_statuses(tmp_path):
    corrupt = completed(tmp_path)
    (corrupt.path / "history.ndjson").write_text("tampered\n")
    unsupported = completed(tmp_path)
    manifest = json.loads((unsupported.path / "manifest.json").read_text())
    manifest["bundle_schema_version"] = 9
    (unsupported.path / "manifest.json").write_text(json.dumps(manifest))
    api = client(tmp_path)
    bad = api.get(f"/api/v1/runs/{corrupt.run_id}", headers=AUTH)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "corrupt_evidence"
    newer = api.get(f"/api/v1/runs/{unsupported.run_id}", headers=AUTH)
    assert newer.status_code == 409 and newer.json()["error"]["code"] == "unsupported_version"
    listing = {r["run_id"]: r for r in api.get("/api/v1/runs", headers=AUTH).json()["data"]}
    assert listing[corrupt.run_id]["problem"] == "corrupt_evidence"


def test_groups_are_served_only_from_the_selected_sibling_root(tmp_path):
    groups = tmp_path / "groups"
    groups.mkdir()
    group_id = "3f7ed95a-9052-4cea-a2d2-886ce58376e3"
    (groups / f"{group_id}.json").write_text(
        json.dumps({"group_schema_version": 1, "group_id": group_id, "kind": "replay"})
    )
    assert client(tmp_path).get(f"/api/v1/groups/{group_id}", headers=AUTH).status_code == 200
    assert (
        client(tmp_path, groups=False).get(f"/api/v1/groups/{group_id}", headers=AUTH).status_code
        == 404
    )


# --- artifact robustness (AT-018) ---------------------------------------------------


def test_round_trip_of_a_supported_bundle(tmp_path):
    bundle = completed(tmp_path)
    manifest, report = read_bundle(bundle.path)
    assert report["attempts"][0]["attempt_id"] == "purchase_a/1"
    assert manifest["stage"] == "completed"


def rewrite_history(bundle, transform):
    lines = (bundle.path / "history.ndjson").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    text = transform(events)
    (bundle.path / "history.ndjson").write_text(text)
    reseal(bundle.path)


def test_duplicate_event_identity_is_rejected_even_if_rehashed(tmp_path):
    bundle = completed(tmp_path)

    def duplicate(events):
        events[1]["event_id"] = events[0]["event_id"]
        return "".join(json.dumps(e) + "\n" for e in events)

    rewrite_history(bundle, duplicate)
    with pytest.raises(EvidenceError, match="duplicate"):
        read_bundle(bundle.path)


def test_missing_required_attempt_event_is_rejected(tmp_path):
    bundle = completed(tmp_path)

    def drop_outcome(events):
        events = [e for e in events if e["type"] != "client_outcome"]
        for index, event in enumerate(events, 1):
            event["sequence"] = index
        return "".join(json.dumps(e) + "\n" for e in events)

    rewrite_history(bundle, drop_outcome)
    report = json.loads((bundle.path / "report.json").read_text())
    report["last_sequence"] -= 1
    (bundle.path / "report.json").write_text(json.dumps(report))
    reseal(bundle.path)
    with pytest.raises(EvidenceError, match="missing from the recorded history"):
        read_bundle(bundle.path)


def test_truncated_last_line_and_unsupported_event_version(tmp_path):
    bundle = completed(tmp_path)
    rewrite_history(bundle, lambda events: "".join(json.dumps(e) + "\n" for e in events)[:-1])
    with pytest.raises(EvidenceError, match="truncated"):
        read_bundle(bundle.path)
    other = completed(tmp_path)

    def future(events):
        events[0]["event_schema_version"] = 2
        return "".join(json.dumps(e) + "\n" for e in events)

    rewrite_history(other, future)
    with pytest.raises(UnsupportedArtifact):
        read_bundle(other.path)
