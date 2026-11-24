"""HTTP-level tests: auth, roles, CSRF, idempotency, tenant isolation (G19), end-to-end flow (I05/I08/I09 slices)."""
import pytest

from tests.conftest import login
from tests.engines.collateral.conftest import AS_OF, DEADLINE, lot, simple_policy, uid

P = "/api/v1/collateral"


def toy_policy():
    return simple_policy({"asset_class": "GSEC", "currency": "INR", "valuation_pct": "100"},
                         {"asset_class": "CASH", "currency": "INR", "valuation_pct": "100"})


def import_body(sid, lots=None):
    return {"snapshot_id": sid, "as_of": AS_OF, "currency": "INR", "market_type": "ETD_DOMESTIC", "policy": toy_policy(),
            "lots": lots or [lot("A", "GSEC", "60", txn="1"), lot("B", "CASH", "100", txn="3")]}


def opt_body(mid, sid):
    return {"margin_result_id": mid, "inventory_snapshot_id": sid, "deadline": DEADLINE, "horizon_days": 30}


@pytest.fixture
def maker(app_client):
    return login(app_client, "tenant-a", "maker")


@pytest.fixture
def checker(app_client):
    return login(app_client, "tenant-a", "checker")


def test_engine_is_mounted_and_requires_auth(app_client):
    from fastapi.testclient import TestClient
    anon = TestClient(app_client.app)
    assert anon.get(f"{P}/proposals").status_code == 401
    c = login(app_client, "tenant-a", "viewer")
    caps = c.get("/api/v1/capabilities").json()
    assert next(e for e in caps["engines"] if e["key"] == "collateral")["mounted"] is True
    assert "collateral.optimise" in caps["services"]


def test_roles_viewer_cannot_mutate_admin_cannot_approve(app_client, maker, checker, up):
    viewer = login(app_client, "tenant-a", "viewer")
    admin = login(app_client, "tenant-a", "admin")
    sid = uid("SNAP")
    assert viewer.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")}).status_code == 403
    assert maker.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")}).status_code == 201
    mid = up.add_margin(required_amount={"amount": "100", "currency": "INR"})
    pid = maker.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": uid("k")}).json()["proposal_id"]
    assert viewer.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": uid("k")}).status_code == 403
    for who in (maker, admin, viewer):  # approving needs an independent checker role
        assert who.post(f"{P}/proposals/{pid}/approve", json={"reason": "x"}, headers={"Idempotency-Key": uid("k")}).status_code == 403
    assert checker.post(f"{P}/proposals/{pid}/approve", json={"reason": "reviewed"}, headers={"Idempotency-Key": uid("k")}).status_code == 200


def test_csrf_and_idempotency_key_required(app_client, maker):
    tok = maker.headers.pop("x-csrf-token")
    assert maker.post(f"{P}/inventory/import", json=import_body(uid("S")), headers={"Idempotency-Key": "k"}).status_code == 403
    maker.headers["x-csrf-token"] = tok
    r = maker.post(f"{P}/inventory/import", json=import_body(uid("S")))
    assert r.status_code == 422 and r.json()["field_errors"].get("Idempotency-Key")


def test_idempotent_optimise_and_payload_mismatch(app_client, maker, up):
    sid = uid("SNAP")
    maker.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")})
    mid = up.add_margin(required_amount={"amount": "100", "currency": "INR"})
    key = uid("k")
    a = maker.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": key})
    b = maker.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": key})
    assert a.status_code == 201 and a.json()["proposal_id"] == b.json()["proposal_id"]
    c = maker.post(f"{P}/optimise", json={**opt_body(mid, sid), "horizon_days": 31}, headers={"Idempotency-Key": key})
    assert c.status_code == 409 and c.json()["code"] == "IDEMPOTENCY_MISMATCH"
    n = maker.get(f"{P}/proposals").json()["items"]
    assert len([p for p in n if p["margin_result_id"] == mid]) == 1  # no duplicate effects


def test_tenant_isolation_g19(app_client, maker, up):
    other = login(app_client, "tenant-b", "maker")
    sid = uid("SNAP")
    maker.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")})
    mid = up.add_margin(required_amount={"amount": "100", "currency": "INR"})
    pid = maker.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": uid("k")}).json()["proposal_id"]
    assert other.get(f"{P}/proposals/{pid}").status_code == 404
    assert other.get(f"{P}/proposals/{pid}/instruction").status_code == 404
    assert other.get(f"{P}/inventory/{sid}").status_code == 404
    assert pid not in [p["proposal_id"] for p in other.get(f"{P}/proposals").json()["items"]]
    assert sid not in [s["snapshot_id"] for s in other.get(f"{P}/inventory").json()["items"]]
    chk_b = login(app_client, "tenant-b", "checker")
    assert chk_b.post(f"{P}/proposals/{pid}/approve", json={"reason": "x"}, headers={"Idempotency-Key": uid("k")}).status_code == 404
    r = other.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": uid("k")})
    assert r.status_code == 404  # snapshot belongs to tenant A


def test_end_to_end_http_flow_never_transmits(app_client, maker, checker, up):
    sid = uid("SNAP")
    maker.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")})
    mid = up.add_margin(required_amount={"amount": "100", "currency": "INR"})
    p = maker.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": uid("k")}).json()
    assert p["state"] == "CHECKED" and p["transmission"] == "NOT_TRANSMITTED"
    assert maker.get(f"{P}/proposals/{p['proposal_id']}/instruction").status_code == 404  # nothing drafted yet
    ap = checker.post(f"{P}/proposals/{p['proposal_id']}/approve", json={"reason": "ok", "expected_revision": p["revision"]},
                      headers={"Idempotency-Key": uid("k")}).json()
    res = maker.post(f"{P}/proposals/{p['proposal_id']}/reserve", json={"approval_id": ap["approval"]["approval_id"]},
                     headers={"Idempotency-Key": uid("k")})
    assert res.status_code == 200 and res.json()["state"] == "RESERVED"
    d = maker.post(f"{P}/proposals/{p['proposal_id']}/draft", json={}, headers={"Idempotency-Key": uid("k")}).json()
    assert d["status"] == "NOT_TRANSMITTED" and "NOT TRANSMITTED" in d["content"]["watermark"]
    g = maker.get(f"{P}/proposals/{p['proposal_id']}/instruction").json()
    assert g["content_hash"] == d["content_hash"]
    ev = maker.get("/api/v1/capabilities").json()
    assert ev["external_transmission"] == "DISABLED"
    # evidence trail is readable through the platform run-events? (collateral events are keyed by proposal correlation id)
    # cancel releases and is idempotent
    can = maker.post(f"{P}/proposals/{p['proposal_id']}/cancel", json={"reason": "demo"}, headers={"Idempotency-Key": uid("k")})
    assert can.status_code == 200 and can.json()["state"] == "CANCELLED"


def test_blocked_and_validation_errors_use_envelope(app_client, maker, up):
    sid = uid("SNAP")
    maker.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")})
    mid = up.add_margin(status="BLOCKED", blocking_reasons=["stale market snapshot"])
    r = maker.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": uid("k")})
    assert r.status_code == 409 and r.json()["code"] == "MARGIN_BLOCKED"
    assert set(r.json()) == {"code", "message", "field_errors", "correlation_id", "retryable"}
    bad = maker.post(f"{P}/inventory/import", json={**import_body(uid("S")), "lots": [lot("X", "GSEC", "-1")]},
                     headers={"Idempotency-Key": uid("k")})
    assert bad.status_code == 422 and bad.json()["field_errors"]
    no_ms = maker.post(f"{P}/optimise", json={**opt_body(mid, sid), "horizon_days": 0}, headers={"Idempotency-Key": uid("k")})
    assert no_ms.status_code == 422


def test_infeasible_http_has_no_instruction_and_no_approval(app_client, maker, checker, up):
    sid = uid("SNAP")
    maker.post(f"{P}/inventory/import", json=import_body(sid, [lot("A", "GSEC", "50"), lot("B", "CASH", "30")]),
               headers={"Idempotency-Key": uid("k")})
    mid = up.add_margin(required_amount={"amount": "100", "currency": "INR"})
    p = maker.post(f"{P}/optimise", json=opt_body(mid, sid), headers={"Idempotency-Key": uid("k")}).json()
    assert p["state"] == "INFEASIBLE" and p["result"]["diagnostics"]["remaining"] == "20"
    assert checker.post(f"{P}/proposals/{p['proposal_id']}/approve", json={"reason": "x"}, headers={"Idempotency-Key": uid("k")}).status_code == 409
    assert maker.post(f"{P}/proposals/{p['proposal_id']}/draft", json={}, headers={"Idempotency-Key": uid("k")}).status_code == 409
    assert maker.get(f"{P}/proposals/{p['proposal_id']}/instruction").status_code == 404


def test_stress_endpoint_is_hypothetical(app_client, maker, up):
    sid = uid("SNAP")
    maker.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")})
    mid = up.add_margin(required_amount={"amount": "100", "currency": "INR"})
    r = maker.post(f"{P}/stress", json={**opt_body(mid, sid), "params": {"cost_add_bps": "100"}}, headers={"Idempotency-Key": uid("k")})
    assert r.status_code == 200 and r.json()["hypothetical"] and r.json()["stressed"]["state"] == "HYPOTHETICAL"
    bad = maker.post(f"{P}/stress", json={**opt_body(mid, sid), "params": {"x": 1}}, headers={"Idempotency-Key": uid("k")})
    assert bad.status_code == 422


def test_desk_endpoint(app_client, maker, up):
    sid = uid("SNAP")
    maker.post(f"{P}/inventory/import", json=import_body(sid), headers={"Idempotency-Key": uid("k")})
    mid = up.add_margin(required_amount={"amount": "100", "currency": "INR"})
    r = maker.get(f"{P}/desk", params={"margin_result_id": mid, "inventory_snapshot_id": sid, "deadline": DEADLINE, "horizon_days": 30})
    assert r.status_code == 200 and r.json()["currency"] == "INR" and r.json()["eligible_inventory"]
