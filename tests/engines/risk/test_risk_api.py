"""HTTP-level gates: auth/role/CSRF, tenant isolation (G19), idempotency (G16), envelope errors, actor spoofing, V-A09/V-A10 data."""
import pytest
from sqlalchemy import select

from app.core import services
from app.db.base import SessionLocal
from app.engines.evidence.models import EvEvent
from app.engines.risk import fixtures as fx
from tests.conftest import login

ETD = {"market_type": "ETD_DOMESTIC", "account_id": "ACC-DEMO-NSE-001", "venue": "DEMO_NSE", "as_of": "2026-08-17T04:00:00Z"}


@pytest.fixture(scope="module")
def seeded(app_client):
    with SessionLocal() as s:
        fx.seed_fixtures(s, "tenant-a")
        s.commit()
    return app_client


def body(ctx=None, method="ETD_IMPORTED_V1", snap="SNAP-ETD-001"):
    return {"context": ctx or ETD, "snapshot_id": snap, "methodology": method}


def test_risk_router_mounted_and_capabilities(seeded):
    c = login(seeded, "tenant-a", "viewer")
    caps = c.get("/api/v1/capabilities").json()
    assert next(e for e in caps["engines"] if e["key"] == "risk")["mounted"] is True
    assert "risk.compute_margin" in caps["services"]
    m = c.get("/api/v1/risk/methodology").json()
    assert "OTC_VM_SIMPLE_V1" in m["methods"] and "limitations" in m["methods"]["OTC_VM_SIMPLE_V1"]


def test_unauthenticated_and_viewer_cannot_write(seeded):
    from fastapi.testclient import TestClient
    assert TestClient(seeded.app).get("/api/v1/risk/results").status_code == 401
    v = login(seeded, "tenant-a", "viewer")
    assert v.post("/api/v1/risk/calculate", json=body(), headers={"Idempotency-Key": "k"}).status_code == 403
    assert v.post("/api/v1/risk/snapshots", json={"snapshot_id": "X", "data_as_of": "2026-08-17T00:00:00Z", "payload": {}}).status_code == 403
    assert v.get("/api/v1/risk/snapshots").status_code == 200


def test_idempotency_required_and_replayed(seeded):
    m = login(seeded, "tenant-a", "maker")
    r = m.post("/api/v1/risk/calculate", json=body())
    assert r.status_code == 422 and set(r.json()) == {"code", "message", "field_errors", "correlation_id", "retryable"}
    a = m.post("/api/v1/risk/calculate", json=body(), headers={"Idempotency-Key": "idem-1"})
    b = m.post("/api/v1/risk/calculate", json=body(), headers={"Idempotency-Key": "idem-1"})
    assert a.status_code == 200 and a.json()["status"] == "OK" and a.json() == b.json()
    assert a.json()["required_amount"] == {"amount": "10000000", "currency": "INR"}
    c = m.post("/api/v1/risk/calculate", json=body(snap="SNAP-IFSC-001"), headers={"Idempotency-Key": "idem-1"})
    assert c.status_code == 409  # same key, different payload


def test_tenant_isolation_results_and_inputs(seeded):
    a, b = login(seeded, "tenant-a", "maker"), login(seeded, "tenant-b", "maker")
    rid = a.post("/api/v1/risk/calculate", json=body(), headers={"Idempotency-Key": "iso-1"}).json()["result_id"]
    assert a.get(f"/api/v1/risk/results/{rid}").status_code == 200
    assert b.get(f"/api/v1/risk/results/{rid}").status_code == 404
    assert rid not in [x["result_id"] for x in b.get("/api/v1/risk/results").json()["items"]]
    assert b.get("/api/v1/risk/snapshots").json()["items"] == []
    assert b.get("/api/v1/risk/trades").json()["items"] == []
    # tenant B cannot calculate on tenant A's snapshot: BLOCKED (snapshot not found), no leakage
    r = b.post("/api/v1/risk/calculate", json=body(), headers={"Idempotency-Key": "iso-2"}).json()
    assert r["status"] == "BLOCKED" and "not found" in r["blocking_reasons"][0]
    assert b.post("/api/v1/risk/stress", json={"base_result_id": rid, "shock_spec": {"price_shock_pct": "-0.05"}}).status_code == 404


def test_actor_spoofing_in_context_is_ignored(seeded):
    m = login(seeded, "tenant-a", "maker")
    ctx = dict(ETD, actor_id="attacker", correlation_id="forged", as_of="2026-08-17T04:00:01Z")
    rid = m.post("/api/v1/risk/calculate", json=body(ctx), headers={"Idempotency-Key": "spoof-1"}).json()["result_id"]
    with SessionLocal() as s:
        ev = s.execute(select(EvEvent).where(EvEvent.tenant_id == "tenant-a", EvEvent.entity_id == rid)).scalars().all()
    assert ev and all(e.actor_id != "attacker" and e.correlation_id != "forged" for e in ev)


def test_otc_blocks_with_reason_when_contracts_unavailable(seeded, monkeypatch):
    monkeypatch.delitem(services._REGISTRY, "contracts.resolve_terms", raising=False)
    m = login(seeded, "tenant-a", "maker")
    ctx = {"market_type": "OTC", "netting_set_id": "NS-MER-NWD-001", "as_of": "2026-08-17T04:00:00Z", "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": "0"}}
    r = m.post("/api/v1/risk/calculate", json=body(ctx, "OTC_VM_SIMPLE_V1", "SNAP-SHOCK-001"), headers={"Idempotency-Key": "otc-1"}).json()
    assert r["status"] == "BLOCKED" and r["required_amount"] is None and r["blocking_reasons"]
    t = m.post("/api/v1/risk/terms/resolve", json={"netting_set_id": "NS-MER-NWD-001", "as_of": "2026-08-17T04:00:00Z"}).json()
    assert t["status"] == "BLOCKED" and t["terms_hash"] is None


def test_ccp_otc_unsupported_over_api(seeded):
    m = login(seeded, "tenant-a", "maker")
    r = m.post("/api/v1/risk/calculate", json=body({"market_type": "CCP_OTC", "as_of": "2026-08-17T04:00:00Z"}, "OTC_VM_SIMPLE_V1"), headers={"Idempotency-Key": "ccp-1"}).json()
    assert r["status"] == "UNSUPPORTED"


def test_import_validation_errors_use_envelope(seeded):
    m = login(seeded, "tenant-a", "maker")
    r = m.post("/api/v1/risk/trades/import", json={"trades": [{"trade_id": "Z", "master_id": "M", "type": "IRS"}]})
    assert r.status_code == 422 and r.json()["field_errors"]
    csv = (fx.FIX / "clearing_nse_001.csv").read_text()
    dup = csv + csv.splitlines()[1] + "\n"
    r2 = m.post("/api/v1/risk/clearing/import", json={"csv_text": dup, "venue": "DEMO_NSE", "revision": 9})
    assert r2.status_code == 422 and any("duplicate" in v for v in r2.json()["field_errors"].values())
    r3 = m.post("/api/v1/risk/clearing/import", json={"csv_text": csv, "venue": "DEMO_NSE", "expected_sha256": "1" * 64})
    assert r3.status_code == 422
    ok = m.post("/api/v1/risk/clearing/import", json={"csv_text": csv, "venue": "DEMO_NSE"})
    assert ok.status_code == 201 and ok.json()["status"] == "RECONCILED"


def test_reconcile_and_stress_endpoints(seeded):
    m = login(seeded, "tenant-a", "maker")
    rec = m.post("/api/v1/risk/reconcile", json={"file_id": "CLR-DEMO-NSE-BADTOTAL", "snapshot_id": "SNAP-ETD-001"}).json()
    assert rec["status"] == "EXCEPTION" and rec["difference"]["amount"] == "100000"
    rid = m.post("/api/v1/risk/calculate", json=body(), headers={"Idempotency-Key": "st-base"}).json()["result_id"]
    st = m.post("/api/v1/risk/stress", json={"base_result_id": rid, "shock_spec": {"price_shock_pct": "-0.05"}})
    assert st.status_code == 200 and st.json()["baseline_mutated"] is False
    assert m.get(f"/api/v1/risk/stress/{st.json()['stress_id']}").status_code == 200
    bad = m.post("/api/v1/risk/stress", json={"base_result_id": rid, "shock_spec": {"price_shock_pct": 0.05}})  # float rejected
    assert bad.status_code == 422


# ---------------------------------------------------------------- round 2: contract v1.1 over HTTP
def test_v11_position_metrics_and_reconciliation_status_endpoints(seeded):
    v = login(seeded, "tenant-a", "viewer")
    g = v.get("/api/v1/risk/position-metrics", params={"account_id": "ACC-DEMO-NSE-001", "as_of": "2026-08-17T04:00:00Z"})
    assert g.status_code == 200
    j = g.json()
    assert j["gross_lots"] == "60" and j["notional"] == "36000000.00" and j["client_class"] == "UNKNOWN"
    r = v.get("/api/v1/risk/reconciliation-status", params={"account_id": "ACC-DEMO-NSE-001", "as_of": "2026-08-17T04:00:00Z"}).json()
    assert r["status"] == "OK" and r["expected_total"] == "10000000"
    u = v.get("/api/v1/risk/reconciliation-status", params={"account_id": "ACC-NOBODY", "as_of": "2026-08-17T04:00:00Z"}).json()
    assert u["status"] == "UNKNOWN"
    assert v.get("/api/v1/risk/position-metrics", params={"account_id": "ACC-DEMO-NSE-001", "as_of": "not-a-date"}).status_code == 422
    assert v.get("/api/v1/risk/position-metrics", params={"account_id": "ACC-DEMO-NSE-001"}).status_code == 422  # as_of is mandatory: no implicit clock


def test_v11_endpoints_are_tenant_scoped_and_authenticated(seeded):
    from fastapi.testclient import TestClient
    assert TestClient(seeded.app).get("/api/v1/risk/position-metrics", params={"account_id": "ACC-DEMO-NSE-001", "as_of": "2026-08-17T04:00:00Z"}).status_code == 401
    b = login(seeded, "tenant-b", "viewer")  # tenant B has no seeded risk data: must see NOTHING of tenant A's account
    j = b.get("/api/v1/risk/position-metrics", params={"account_id": "ACC-DEMO-NSE-001", "as_of": "2026-08-17T04:00:00Z"}).json()
    assert j["available"] is False and j["gross_lots"] is None
    assert b.get("/api/v1/risk/reconciliation-status", params={"account_id": "ACC-DEMO-NSE-001", "as_of": "2026-08-17T04:00:00Z"}).json()["status"] == "UNKNOWN"


def test_terms_resolve_reports_the_known_at_it_used(seeded, monkeypatch):
    seen = {}

    def provider(session, tenant_id, ns, as_of, known_at, fact_snapshot_id=None):
        seen["known_at"] = known_at
        return {"status": "RESOLVED", "terms_hash": "h" * 64, "terms": {"base_currency": "USD"}, "blocking_reasons": []}
    monkeypatch.setitem(services._REGISTRY, "contracts.resolve_terms", provider)
    m = login(seeded, "tenant-a", "maker")
    a = m.post("/api/v1/risk/terms/resolve", json={"netting_set_id": "NS-MER-NWD-001", "as_of": "2026-08-17T04:00:00Z"}).json()
    assert a["known_at_source"] == "SERVER_RECORDING_TIME" and a["known_at"] > "2026-08-17T04:00:00"  # recording time is server time, not as_of
    b = m.post("/api/v1/risk/terms/resolve", json={"netting_set_id": "NS-MER-NWD-001", "as_of": "2026-08-17T04:00:00Z", "known_at": "2026-08-18T00:00:00Z"}).json()
    assert b["known_at_source"] == "CALLER" and seen["known_at"].isoformat().startswith("2026-08-18T00:00:00")
