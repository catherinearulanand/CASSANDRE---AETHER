"""HTTP-level tests: auth/roles/CSRF, idempotency, maker-checker over the API, tenant isolation (G19), envelopes."""
import uuid

import pytest

from app.engines.contracts import fixtures as F
from tests.conftest import login

H = lambda: {"Idempotency-Key": str(uuid.uuid4())}  # noqa: E731
BASE = "/api/v1/contracts"


@pytest.fixture(scope="module")
def flow(app_client):
    """maker creates + extracts, checker approves every field, maker proposes, checker freezes (all over HTTP)."""
    maker, checker = login(app_client, "tenant-a", "maker"), login(app_client, "tenant-a", "checker")
    body = F.package_body(next(s for s in F.SPECS if s["netting_set_id"] == "NS-DEMO-G25"))
    key = str(uuid.uuid4())
    r = maker.post(f"{BASE}/packages", json=body, headers={"Idempotency-Key": key})
    assert r.status_code == 201, r.text
    pid = r.json()["package_id"]
    return {"maker": maker, "checker": checker, "pid": pid, "body": body, "key": key, "created": r.json()}


def test_idempotent_create_and_payload_mismatch(app_client, flow):
    r2 = flow["maker"].post(f"{BASE}/packages", json=flow["body"], headers={"Idempotency-Key": flow["key"]})
    assert r2.status_code == 201 and r2.json()["package_id"] == flow["pid"]  # same key, same effect (G16)
    other = dict(flow["body"], governing_law="Other Law")
    assert flow["maker"].post(f"{BASE}/packages", json=other, headers={"Idempotency-Key": flow["key"]}).status_code == 409
    n = flow["maker"].get(f"{BASE}/packages").json()["items"]
    assert sum(1 for p in n if p["netting_set_id"] == "NS-DEMO-G25") == 1


def test_missing_idempotency_key_is_422_with_envelope(app_client, flow):
    r = flow["maker"].post(f"{BASE}/packages", json=flow["body"])
    assert r.status_code == 422 and set(r.json()) == {"code", "message", "field_errors", "correlation_id", "retryable"}


def test_validation_error_envelope_for_bad_package(app_client):
    m = login(app_client, "tenant-a", "maker")
    r = m.post(f"{BASE}/packages", json={"master_id": "x"}, headers=H())
    assert r.status_code == 422 and r.json()["field_errors"]


def test_viewer_can_read_but_not_mutate(app_client, flow):
    v = login(app_client, "tenant-a", "viewer")
    assert v.get(f"{BASE}/{flow['pid']}").status_code == 200
    assert v.post(f"{BASE}/{flow['pid']}/extract", json={"mode": "RULES"}, headers=H()).status_code == 403
    assert v.post(f"{BASE}/packages", json=flow["body"], headers=H()).status_code == 403


def test_csrf_enforced(app_client, flow):
    m = login(app_client, "tenant-a", "maker")
    m.headers.pop("x-csrf-token")
    assert m.post(f"{BASE}/{flow['pid']}/extract", json={"mode": "RULES"}, headers=H()).status_code == 403


def test_live_model_is_501(app_client, flow):
    r = flow["maker"].post(f"{BASE}/{flow['pid']}/extract", json={"mode": "LIVE_MODEL"}, headers=H())
    assert r.status_code == 501 and r.json()["code"] == "NOT_IMPLEMENTED"


def test_full_review_freeze_resolve_over_http(app_client, flow):
    m, c, pid = flow["maker"], flow["checker"], flow["pid"]
    r = m.post(f"{BASE}/{pid}/extract", json={"mode": "RULES"}, headers=H())
    assert r.status_code == 200 and r.json()["fields_created"] > 0 and r.json()["unresolved"] == 0
    terms = m.get(f"{BASE}/{pid}/terms").json()
    assert not terms["readiness"]["ready_to_freeze"] and terms["readiness"]["blocking_reasons"]
    # maker can't approve (role), checker can
    f0 = terms["fields"][0]
    assert m.post(f"{BASE}/{pid}/review", json={"field_id": f0["field_id"], "action": "APPROVE", "reason": "maker approving",
                                                 "expected_revision": f0["revision"]}, headers=H()).status_code == 403
    for f in terms["fields"]:
        rr = c.post(f"{BASE}/{pid}/review", json={"field_id": f["field_id"], "action": "APPROVE",
                                                   "reason": "verified against quote", "expected_revision": f["revision"]}, headers=H())
        assert rr.status_code == 200, rr.text
    # revision conflict
    assert c.post(f"{BASE}/{pid}/review", json={"field_id": f0["field_id"], "action": "REJECT", "reason": "stale view here",
                                                 "expected_revision": f0["revision"]}, headers=H()).status_code == 409
    # freeze: checker cannot propose, maker cannot freeze
    assert c.post(f"{BASE}/{pid}/freeze/propose", json={"reason": "checker proposing"}, headers=H()).status_code == 403
    ap = m.post(f"{BASE}/{pid}/freeze/propose", json={"reason": "propose exact approved terms"}, headers=H())
    assert ap.status_code == 200, ap.text
    assert m.post(f"{BASE}/{pid}/freeze", json={"approval_id": ap.json()["approval_id"], "reason": "maker freezing"},
                  headers=H()).status_code == 403
    fz = c.post(f"{BASE}/{pid}/freeze", json={"approval_id": ap.json()["approval_id"], "reason": "checker approves exact hash"}, headers=H())
    assert fz.status_code == 200 and fz.json()["version_no"] == 1
    # resolve through the HTTP surface of the frozen contract
    res = m.get(f"{BASE}/resolve", params={"netting_set_id": "NS-DEMO-G25", "as_of": "2026-08-17T04:00:00Z"}).json()
    assert res["status"] == "RESOLVED" and res["terms"]["mta_a"] == "10" and res["terms"]["rounding_increment"] == "1"
    assert m.get(f"{BASE}/terms-by-hash/{res['terms_hash']}").json()["terms"] == res["terms"]
    # frozen => read-only
    assert c.post(f"{BASE}/{pid}/review", json={"field_id": f0["field_id"], "action": "REJECT", "reason": "edit frozen field",
                                                 "expected_revision": f0["revision"] + 1}, headers=H()).status_code == 409
    vers = m.get(f"{BASE}/{pid}/versions").json()["items"]
    assert len(vers) == 1 and vers[0]["checker_id"] != vers[0]["maker_id"]
    # source viewer data
    doc = m.get(f"{BASE}/{pid}").json()["document_list"][0]
    d = m.get(f"{BASE}/{pid}/documents/{doc['doc_id']}").json()
    assert d["text"][d["spans"][0]["start"]:d["spans"][0]["end"]]


def test_tenant_isolation_g19(app_client, flow):
    b = login(app_client, "tenant-b", "maker")
    pid = flow["pid"]
    for path in (f"{BASE}/{pid}", f"{BASE}/{pid}/terms", f"{BASE}/{pid}/versions", f"{BASE}/{pid}/netting"):
        assert b.get(path).status_code == 404
    assert b.post(f"{BASE}/{pid}/extract", json={"mode": "RULES"}, headers=H()).status_code == 404
    assert b.get(f"{BASE}/packages").json()["items"] == []
    res = b.get(f"{BASE}/resolve", params={"netting_set_id": "NS-DEMO-G25", "as_of": "2026-08-17T04:00:00Z"}).json()
    assert res["status"] == "BLOCKED" and res["terms"] is None


def test_netting_import_over_http(app_client, flow):
    m = flow["maker"]
    r = m.post(f"{BASE}/netting/import", json={"trades": [
        {"trade_id": "API-T1", "declared_master_id": "MA-DEMO-G25", "declared_netting_set_id": "NS-DEMO-G25"},
        {"trade_id": "API-T2", "declared_master_id": "MA-WRONG", "declared_netting_set_id": "NS-DEMO-G25"},
        {"trade_id": "API-T3", "declared_master_id": "MA-DEMO-G25", "declared_netting_set_id": None}]}, headers=H())
    assert r.status_code == 200, r.text
    assert r.json()["counts"] == {"MEMBER": 1, "QUARANTINED": 1, "REJECTED": 1}
    assert m.get(f"{BASE}/netting/membership/API-T2").json()["netting_set_id"] is None
    assert len(m.get(f"{BASE}/netting/quarantine").json()["items"]) == 2
    assert {x["trade_id"] for x in m.get(f"{BASE}/{flow['pid']}/netting").json()["items"]} >= {"API-T1", "API-T2"}


def test_resolve_rejects_bad_datetime(app_client, flow):
    r = flow["maker"].get(f"{BASE}/resolve", params={"netting_set_id": "NS-DEMO-G25", "as_of": "not-a-date"})
    assert r.status_code == 422


def test_facts_need_explicit_source(app_client, flow):
    m = flow["maker"]
    bad = m.post(f"{BASE}/facts", json={"facts": {"rating:X": {"value": "A", "effective_at": "2026-01-01T00:00:00Z"}}}, headers=H())
    assert bad.status_code == 422
    ok = m.post(f"{BASE}/facts", json={"facts": {"rating:X": {"value": "A", "source_id": "SYNTH-1", "effective_at": "2026-01-01T00:00:00Z"}}}, headers=H())
    assert ok.status_code == 201 and ok.json()["fact_snapshot_id"]
