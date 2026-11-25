import uuid

import pytest
from sqlalchemy import select
from tests.conftest import login
from tests.engines.compliance.conftest import CTX

from app.db.base import SessionLocal
from app.engines.evidence import service as evsvc
from app.engines.evidence.models import EvEvent

pytestmark = pytest.mark.security
API = "/api/v1/compliance"


def H(k=None):
    return {"Idempotency-Key": k or uuid.uuid4().hex}


@pytest.fixture(scope="module")
def users(app_client):
    from app.auth.security import hash_password
    from app.db.models_core import User
    with SessionLocal() as s:
        for t in ("tenant-a", "tenant-b"):
            s.add(User(tenant_id=t, username="officer", display_name="officer", password_hash=hash_password("pw-officer"),
                       roles=["compliance_officer"], entitlements=[]))
        s.commit()


@pytest.fixture
def seeded(app_client, users, up):
    a = login(app_client, "tenant-a", "admin")
    assert a.post(f"{API}/controls/seed-library").status_code == 201
    return a


def eval_body(snap=None, **kw):
    return {"context": CTX, "snapshot_id": snap or uuid.uuid4().hex[:8], "rule_set_hash": "rsh", "margin_result_id": "MR-1", **kw}


def test_requires_auth_and_roles(app_client, seeded):
    from fastapi.testclient import TestClient
    anon = TestClient(app_client.app)
    assert anon.get(f"{API}/cases").status_code == 401
    viewer = login(app_client, "tenant-a", "viewer")
    assert viewer.get(f"{API}/cases").status_code == 200 and viewer.get(f"{API}/controls").status_code == 200
    assert viewer.post(f"{API}/evaluate", json=eval_body(), headers=H()).status_code == 403
    assert viewer.post(f"{API}/surveillance/run", json={"seed": 42}, headers=H()).status_code == 403
    checker = login(app_client, "tenant-a", "checker")
    assert checker.post(f"{API}/evaluate", json=eval_body(), headers=H()).status_code == 403   # checkers do not author evaluations


def test_csrf_and_idempotency_key_required_and_enforced(app_client, seeded):
    maker = login(app_client, "tenant-a", "maker")
    tok = maker.headers.pop("x-csrf-token")
    assert maker.post(f"{API}/evaluate", json=eval_body(), headers=H()).status_code == 403
    maker.headers["x-csrf-token"] = tok
    r = maker.post(f"{API}/evaluate", json=eval_body())
    assert r.status_code == 422 and r.json()["field_errors"]["Idempotency-Key"] == "required"
    body, key = eval_body("SNAP-IDEM"), uuid.uuid4().hex
    r1 = maker.post(f"{API}/evaluate", json=body, headers=H(key))
    r2 = maker.post(f"{API}/evaluate", json=body, headers=H(key))
    assert r1.status_code == 200 and r1.json() == r2.json()                                  # G16: same key, same result
    assert maker.post(f"{API}/evaluate", json=eval_body("SNAP-OTHER"), headers=H(key)).status_code == 409


def test_G01_through_the_api_with_evidence_and_tenant_isolation(app_client, seeded):
    maker = login(app_client, "tenant-a", "maker")
    r = maker.post(f"{API}/evaluate", json=eval_body(), headers=H())
    assert r.status_code == 200, r.text
    j = r.json()
    cov = [e for e in j["evaluations"] if e["control_key"] == "CMP-COV-ETD-INR"][0]
    assert cov["status"] == "FAIL" and cov["gap"] == "1000000" and len(j["cases"]) == 1
    cid = j["cases"][0]["case_id"]
    detail = maker.get(f"{API}/cases/{cid}").json()
    assert detail["control"]["rule_key"] == "DEMO-ETD-COVERAGE" and detail["remedy_options"] and detail["events"][0]["event_type"] == "OPENED"
    assert detail["separation_note"].startswith("Deterministic")
    # G19: tenant B cannot see tenant A's case, evaluation or lists
    b = login(app_client, "tenant-b", "maker")
    assert b.get(f"{API}/cases/{cid}").status_code == 404
    assert b.get(f"{API}/evaluations/{cov['evaluation_id']}").status_code == 404
    assert b.post(f"{API}/cases/{cid}/transition", json={"to_state": "ACKNOWLEDGED", "expected_revision": 1}, headers=H()).status_code == 404
    assert cid not in [c["case_id"] for c in b.get(f"{API}/cases").json()["items"]]
    # evidence: every consequential write is on the chain, and the chain verifies
    with SessionLocal() as s:
        acts = {e.action for e in s.execute(select(EvEvent).where(EvEvent.tenant_id == "tenant-a", EvEvent.engine == "compliance")).scalars()}
        assert {"CONTROL_EVALUATED", "CASE_OPENED"} <= acts
        assert evsvc.verify_chain(s, "tenant-a")["valid"]


def test_case_transition_api_optimistic_locking_and_roles(app_client, seeded):
    maker = login(app_client, "tenant-a", "maker")
    cid = maker.post(f"{API}/evaluate", json=eval_body(), headers=H()).json()["cases"][0]["case_id"]
    c = maker.get(f"{API}/cases/{cid}").json()
    stale = maker.post(f"{API}/cases/{cid}/transition", json={"to_state": "ACKNOWLEDGED", "expected_revision": c["revision"] + 3}, headers=H())
    assert stale.status_code == 409 and stale.json()["code"] == "REVISION_CONFLICT"
    ok = maker.post(f"{API}/cases/{cid}/transition", json={"to_state": "ACKNOWLEDGED", "expected_revision": c["revision"]}, headers=H())
    assert ok.status_code == 200 and ok.json()["state"] == "ACKNOWLEDGED" and ok.json()["owner_id"]
    viewer = login(app_client, "tenant-a", "viewer")
    assert viewer.post(f"{API}/cases/{cid}/transition", json={"to_state": "INVESTIGATING", "expected_revision": ok.json()["revision"]}, headers=H()).status_code == 403
    assert maker.post(f"{API}/cases/{cid}/transition", json={"to_state": "RESOLVED", "reason": "trying to skip", "expected_revision": ok.json()["revision"]},
                      headers=H()).status_code == 409


def test_full_closure_flow_over_http_G15_style_separation(app_client, seeded, up):
    maker, checker = login(app_client, "tenant-a", "maker"), login(app_client, "tenant-a", "checker")
    fresh = {**CTX, "account_id": "ACC-FLOW-" + uuid.uuid4().hex[:6]}      # own account => own case (dedupe is per subject)
    cid = maker.post(f"{API}/evaluate", json=eval_body(context=fresh), headers=H()).json()["cases"][0]["case_id"]

    def step(cl, to, reason=None):
        c = cl.get(f"{API}/cases/{cid}").json()
        r = cl.post(f"{API}/cases/{cid}/transition", json={"to_state": to, "reason": reason, "expected_revision": c["revision"]}, headers=H())
        assert r.status_code == 200, r.text
        return r.json()
    step(maker, "ACKNOWLEDGED"); step(maker, "INVESTIGATING")
    c = maker.get(f"{API}/cases/{cid}").json()
    rem = maker.post(f"{API}/cases/{cid}/remediate", json={"expected_revision": c["revision"], "selected": "ROUTE_COLLATERAL_OPTIMISE",
                                                          "rationale": "Ask ATLAS for a feasible allocation"}, headers=H())
    assert rem.status_code == 200 and rem.json()["state"] == "REMEDIATION_PROPOSED"
    step(maker, "REVIEW_REQUIRED")
    c = maker.get(f"{API}/cases/{cid}").json()
    r = checker.post(f"{API}/cases/{cid}/transition", json={"to_state": "RESOLVED", "reason": "attempt without recheck", "expected_revision": c["revision"]}, headers=H())
    assert r.status_code == 409 and r.json()["code"] == "RECHECK_REQUIRED"
    still = maker.post(f"{API}/cases/{cid}/recheck", json={"snapshot_id": "RC-1", "as_of": "2026-08-17T06:00:00Z"}, headers=H())
    assert still.status_code == 200 and still.json()["status"] == "FAIL"
    up.posted = {"amount": "12500000", "currency": "INR"}
    good = maker.post(f"{API}/cases/{cid}/recheck", json={"snapshot_id": "RC-2", "as_of": "2026-08-17T07:00:00Z"}, headers=H())
    assert good.json()["status"] == "PASS"
    maker_try = maker.post(f"{API}/cases/{cid}/transition", json={"to_state": "RESOLVED", "reason": "closing my own case",
                                                                  "expected_revision": maker.get(f"{API}/cases/{cid}").json()["revision"]}, headers=H())
    assert maker_try.status_code == 403
    done = step(checker, "RESOLVED", "Verified by recheck PASS")
    assert done["state"] == "RESOLVED" and done["resolution"]["approved_by"]
    up.posted = {"amount": "11500000", "currency": "INR"}


def test_waiver_endpoints_enforce_maker_checker(app_client, seeded):
    maker, checker = login(app_client, "tenant-a", "maker"), login(app_client, "tenant-a", "checker")
    w = maker.post(f"{API}/waivers", json={"control_key": "CMP-RECON-ETD", "subject_id": "ACC-W-1", "reason": "feed outage, internal policy",
                                           "valid_from": "2026-08-17T00:00:00Z", "valid_to": "2026-08-19T00:00:00Z"}, headers=H())
    assert w.status_code == 201, w.text
    wid = w.json()["waiver_id"]
    assert maker.post(f"{API}/waivers/{wid}/decision", json={"decision": "APPROVE", "reason": "self"}, headers=H()).status_code == 403
    ok = checker.post(f"{API}/waivers/{wid}/decision", json={"decision": "APPROVE", "reason": "independent review ok"}, headers=H())
    assert ok.status_code == 200 and ok.json()["status"] == "ACTIVE"
    assert login(app_client, "tenant-b", "checker").post(f"{API}/waivers/{wid}/decision", json={"decision": "REJECT", "reason": "other tenant"},
                                                         headers=H()).status_code == 404


def test_controls_endpoint_reports_coverage_denominator_not_percentage(app_client, seeded):
    j = login(app_client, "tenant-a", "viewer").get(f"{API}/controls").json()
    c = j["coverage"]
    assert c["registered"] == 10 and c["unsupported"] == 0 and "unknown_count" in c and "latest_status_counts" in c
    assert not any("percent" in k or "score" in k for k in c)


def test_queue_requires_explicit_clock_or_uses_latest_evaluation(app_client, seeded):
    maker = login(app_client, "tenant-a", "maker")
    maker.post(f"{API}/evaluate", json=eval_body(), headers=H())
    q = maker.get(f"{API}/queue", params={"as_of": "2026-08-19T00:00:00Z"}).json()
    assert q["items"] and all("sla" in i for i in q["items"]) and "never the server clock" in q["clock_note"]


def test_surveillance_run_and_view_over_http(app_client, seeded):
    officer = login(app_client, "tenant-a", "officer")
    r = officer.post(f"{API}/surveillance/run", json={"seed": 42}, headers=H())
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["cases_opened"] > 0 and "not a finding of misconduct" in run["disclaimer"]
    v = officer.get(f"{API}/surveillance").json()
    m = v["model"]
    assert m["model_card"]["limitations"] and "confusion_matrix" in m["metrics"]["primary"]["holdout"] and m["status"] == "ACTIVE"
    assert v["scores"] and v["scores"][0]["top_features"]
    assert officer.get(f"{API}/models/{run['model_id']}").status_code == 200
    assert login(app_client, "tenant-b", "viewer").get(f"{API}/models/{run['model_id']}").status_code == 404
    cases = officer.get(f"{API}/cases", params={"kind": "ANOMALY_INVESTIGATION"}).json()["items"]
    assert cases and all(c["last_status"] == "ANOMALY_SIGNAL" for c in cases)


def test_register_control_compiles_or_rejects(app_client, seeded):
    officer = login(app_client, "tenant-a", "officer")
    bad = {"control_key": "X-BAD", "family": "POSITION_LIMIT", "title": "t", "rule_key": "R", "scope": {"eq": ["market_type", "OTC"]},
           "condition": {"lte": ["position_lots"]}}
    assert officer.post(f"{API}/controls", json=bad, headers=H()).status_code == 422          # G29: cannot compile
    good = {**bad, "control_key": "X-GOOD", "condition": {"lte": ["position_lots", 5]}}
    r = officer.post(f"{API}/controls", json=good, headers=H())
    assert r.status_code == 201 and len(r.json()["rule_hash"]) == 64
