import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import login

BASE = "/api/v1/regulatory"
DOC = b"1. Limits\n1.1 A client shall not exceed 25 lots of net position on DEMO_NSE.\n"
CTX = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "client_classification": "CLIENT", "net_position_lots": "10"}


def idem():
    return {"Idempotency-Key": uuid.uuid4().hex}


def upload(c, data=DOC, title="API doc", auth="SYNTHETIC", ctype="text/plain", headers=None):
    return c.post(f"{BASE}/documents", files={"file": ("n.txt", data, ctype)}, data={"authority_type": auth, "title": title}, headers=headers or idem())


@pytest.fixture(scope="module")
def clients(app_client):
    return (login(app_client, "tenant-a", "maker"), login(app_client, "tenant-a", "checker"),
            login(app_client, "tenant-a", "viewer"), login(app_client, "tenant-b", "maker"))


def rule_body(span_id, key):
    return {"rule_key": key, "authority_type": "SYNTHETIC_POLICY", "title": "API rule",
            "scope": {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["venue", "DEMO_NSE"]}]},
            "condition": {"all": [{"in": ["client_classification", ["CLIENT"]]}, {"lte": ["net_position_lots", {"param": "limit"}]}]},
            "parameters": {"limit": {"value": "25", "unit": "LOTS"}}, "span_ids": [span_id], "effective_from": "2026-01-01T00:00:00Z",
            "rationale": "API flow", "test_vectors": [
                {"name": "ok", "context": CTX, "expect_scope": "TRUE", "expect_condition": "TRUE"},
                {"name": "over", "context": CTX | {"net_position_lots": "26"}, "expect_scope": "TRUE", "expect_condition": "FALSE"}]}


def test_i03_end_to_end_source_to_extraction_to_review_to_activation_to_resolution(clients):
    maker, checker, viewer, other = clients
    key = "API-" + uuid.uuid4().hex[:6]
    doc = upload(maker, DOC + key.encode() + b"\n")
    assert doc.status_code == 201, doc.text
    sid = doc.json()["source_id"]
    ex = maker.post(f"{BASE}/documents/{sid}/extract", json={"mode": "DETERMINISTIC"}, headers=idem())
    assert ex.status_code == 201 and ex.json()["candidates"][0]["fields"]["value"] == "25"
    cand = ex.json()["candidates"][0]
    assert cand["grounded"] and cand["confidence_category"] == "PATTERN_MATCH"
    r = maker.post(f"{BASE}/rules", json=rule_body(cand["span_ids"][0], key), headers=idem())
    assert r.status_code == 201, r.text
    rid, rev = r.json()["rule_id"], r.json()["revision"]
    assert r.json()["compile_ok"] and r.json()["status"] == "DRAFT"
    sub = maker.post(f"{BASE}/rules/{rid}/review", json={"decision": "SUBMIT", "reason": "ready", "expected_revision": rev}, headers=idem())
    assert sub.status_code == 200 and sub.json()["status"] == "REVIEW_REQUIRED", sub.text
    # G15 over HTTP: the maker cannot approve; the denial is recorded
    bad = maker.post(f"{BASE}/rules/{rid}/review", json={"decision": "APPROVE", "reason": "me", "expected_revision": sub.json()["revision"]}, headers=idem())
    assert bad.status_code == 403 and bad.json()["code"] == "SELF_APPROVAL"
    from app.db.base import SessionLocal
    from app.engines.evidence.models import EvEvent
    with SessionLocal() as s:
        assert s.execute(select(EvEvent).where(EvEvent.tenant_id == "tenant-a", EvEvent.entity_id == rid, EvEvent.action == "APPROVAL_DENIED")).first()
    stale = checker.post(f"{BASE}/rules/{rid}/review", json={"decision": "APPROVE", "reason": "x", "expected_revision": 1}, headers=idem())
    assert stale.status_code == 409 and stale.json()["code"] == "REVISION_CONFLICT"
    ok = checker.post(f"{BASE}/rules/{rid}/review", json={"decision": "APPROVE", "reason": "independent check", "expected_revision": sub.json()["revision"]}, headers=idem())
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED", ok.text
    assert viewer.post(f"{BASE}/rules/{rid}/activate", json={"reason": "x"}, headers=idem()).status_code == 403
    act = checker.post(f"{BASE}/rules/{rid}/activate", json={"reason": "activate", "expected_revision": ok.json()["revision"], "as_of": "2026-08-17T04:00:00Z"}, headers=idem())
    assert act.status_code == 200 and act.json()["status"] == "ACTIVE", act.text
    res = viewer.post(f"{BASE}/resolve", json={"context": CTX, "as_of": "2026-08-17T04:00:00Z"}).json()
    assert key in [e["rule_key"] for e in res["applicable"]] and res["official"] is True
    detail = viewer.get(f"{BASE}/rules/{rid}").json()
    assert detail["approvals"][0]["decision"] == "APPROVED" and detail["spans"][0]["quote"].startswith("A client shall not exceed") and len(detail["tests"]) == 2
    assert any(x["rule_id"] == rid for x in viewer.get(f"{BASE}/rules?status=ACTIVE").json()["items"])
    # I12/G19 tenant B sees nothing of tenant A
    assert other.get(f"{BASE}/rules/{rid}").status_code == 404
    assert other.get(f"{BASE}/documents/{sid}").status_code == 404
    assert other.get(f"{BASE}/documents/{sid}/spans").status_code == 404
    assert other.post(f"{BASE}/documents/{sid}/extract", json={"mode": "DETERMINISTIC"}, headers=idem()).status_code == 404
    assert key not in [x["rule_key"] for x in other.get(f"{BASE}/rules").json()["items"]]
    assert other.post(f"{BASE}/resolve", json={"context": CTX, "as_of": "2026-08-17T04:00:00Z"}).json()["applicable"] == []
    # amendment diff over HTTP
    rev = maker.post(f"{BASE}/rules/{rid}/revise", json={"rationale": "amend limit", "effective_from": "2026-09-01T00:00:00Z"})
    assert rev.status_code == 201
    d = viewer.get(f"{BASE}/rules/{rev.json()['rule_id']}/diff", params={"against": rid})
    assert d.status_code == 200 and d.json()["from"]["version"] == 1 and d.json()["to"]["version"] == 2


def test_auth_roles_csrf_and_envelopes(clients, app_client):
    maker, checker, viewer, _ = clients
    assert upload(viewer, DOC + b"v").status_code == 403
    assert TestClient(app_client.app).get(f"{BASE}/rules").status_code == 401
    m2 = login(app_client, "tenant-a", "maker")
    tok = m2.headers.pop("x-csrf-token")
    assert m2.post(f"{BASE}/compile", json={"scope": {}, "condition": {}}).status_code == 403
    m2.headers["x-csrf-token"] = tok
    r = maker.post(f"{BASE}/rules", json={"rule_key": "X"}, headers=idem())
    assert r.status_code == 422 and set(r.json()) == {"code", "message", "field_errors", "correlation_id", "retryable"}


def test_idempotency_on_upload_same_key_same_result_different_payload_conflict(clients):
    maker = clients[0]
    h = idem()
    a = upload(maker, DOC + b"idem1", headers=h)
    b = upload(maker, DOC + b"idem1", headers=h)
    assert a.status_code == 201 and b.json()["source_id"] == a.json()["source_id"]
    assert upload(maker, DOC + b"idem2", headers=h).status_code == 409
    assert maker.post(f"{BASE}/documents", files={"file": ("n.txt", DOC, "text/plain")}, data={"authority_type": "SYNTHETIC", "title": "t"}).status_code == 422
    dup = upload(maker, DOC + b"idem1")  # new key, same bytes => deduplicated by sha256
    assert dup.json()["source_id"] == a.json()["source_id"] and dup.json()["deduplicated"] is True


def test_upload_validation_errors_are_422_with_reason(clients):
    maker = clients[0]
    r = upload(maker, b"%PDF-1.4 not really a pdf", ctype="text/plain")
    assert r.status_code == 422 and "disagrees" in r.json()["message"]
    assert upload(maker, b"abc", auth="WHATEVER").status_code == 422


def test_extract_blocked_states_are_409_not_silent(clients):
    maker = clients[0]
    sid = upload(maker, DOC + b"blk").json()["source_id"]
    r = maker.post(f"{BASE}/documents/{sid}/extract", json={"mode": "LIVE_MODEL"}, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "LIVE_MODEL_DISABLED"
    r2 = maker.post(f"{BASE}/documents/{sid}/extract", json={"mode": "REPLAY"}, headers=idem())
    assert r2.status_code == 409 and r2.json()["code"] == "NO_REPLAY_FIXTURE"


def test_compile_endpoint_g29_and_field_registry_and_summary(clients):
    maker, _, viewer, _ = clients
    c = maker.post(f"{BASE}/compile", json={"scope": {"eq": ["venue", "X"]}, "condition": {"lte": ["net_position_lots", {"param": "limit"}]},
                                            "parameters": {"limit": {"value": "100"}}}).json()
    assert c["compile"]["ok"] is False and "unit" in str(c["compile"]["errors"])
    reg = viewer.get(f"{BASE}/field-registry").json()
    assert reg["fields"]["net_position_lots"]["unit"] == "LOTS" and "LOTS" in reg["units"]
    s = viewer.get(f"{BASE}/summary").json()
    assert s["data_mode"] == "SYNTHETIC" and "rule_counts" in s and "pending_approvals" in s


def test_resolve_and_impact_endpoints_validation(clients):
    _, _, viewer, _ = clients
    assert viewer.post(f"{BASE}/resolve", json={"context": CTX, "as_of": "2026-08-17T04:00:00"}).status_code == 422  # naive datetime
    assert viewer.post(f"{BASE}/impact", json={"as_of": "2026-08-17T04:00:00Z", "hypothetical_rule_ids": ["x"], "accounts": []}, headers=idem()).status_code == 403
    assert viewer.get(f"{BASE}/impact/does-not-exist").status_code == 404


def test_impact_over_http_blocks_when_risk_missing_and_is_hypothetical(clients):
    maker, checker, _, other = clients
    from tests.engines.regulatory.helpers import fake_services
    from app.db.base import SessionLocal
    from app.engines.regulatory.fixtures import seed_fixtures
    from app.engines.regulatory.models import RegRule
    with SessionLocal() as s:
        seed_fixtures(s, "tenant-a")
        s.commit()
        v2 = s.execute(select(RegRule).where(RegRule.tenant_id == "tenant-a", RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one().rule_id
    with fake_services(**{"risk.compute_margin": None}):
        r = maker.post(f"{BASE}/impact", json={"as_of": "2026-08-17T04:00:00Z", "hypothetical_rule_ids": [v2], "accounts": [
            {"account_id": "ACC-DEMO-NSE-001", "context": {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE"}, "snapshot_id": "SNAP-ETD-001"}]}, headers=idem())
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["status"] == "BLOCKED" and j["official"] is False and "HYPOTHETICAL" in j["banner"]
    assert maker.get(f"{BASE}/impact/{j['impact_id']}").status_code == 200 and other.get(f"{BASE}/impact/{j['impact_id']}").status_code == 404
    assert other.post(f"{BASE}/impact", json={"as_of": "2026-08-17T04:00:00Z", "hypothetical_rule_ids": [v2], "accounts": [{"context": {}}]}, headers=idem()).status_code == 404


def test_g14_conflicting_or_unknown_inputs_block_via_resolve_http(clients):
    _, _, viewer, _ = clients
    res = viewer.post(f"{BASE}/resolve", json={"context": {"market_type": "ETD_DOMESTIC"}, "as_of": "2026-08-17T04:00:00Z"}).json()
    assert isinstance(res["unknown"], list) and isinstance(res["conflicts"], list) and res["rule_set_hash"]
