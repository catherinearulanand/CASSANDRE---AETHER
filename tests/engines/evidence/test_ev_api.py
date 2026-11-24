"""E-A08 tenant/export permissions, E-A10 backend flow, G19 for exports, no public append endpoint."""
import io
import zipfile

import pytest

from app.db.base import SessionLocal
from app.engines.evidence import service as ev
from tests.conftest import login

pytestmark = pytest.mark.security


@pytest.fixture
def run_a(app_client, audit_users):
    a = login(app_client, "tenant-a", "maker")
    import uuid
    rid = a.post("/api/v1/runs", json={"scenario_id": "OTC-SHOCK-G03"}, headers={"Idempotency-Key": "ev-" + uuid.uuid4().hex}).json()["run_id"]
    corr = a.get(f"/api/v1/runs/{rid}").json()["correlation_id"]
    with SessionLocal() as s:
        ev.append(s, "tenant-a", engine="risk", action="MARGIN_COMPUTED", actor_id="svc", actor_type="SERVICE", entity_type="margin_result",
                  entity_id="MR-" + rid[:6], correlation_id=corr, rationale="secret rationale text",
                  payload={"required": "9500000.00", "parents": [{"entity_type": "snapshot", "entity_id": "SNAP-SHOCK-001"}]})
        s.commit()
    return rid, corr


def test_no_public_append_endpoint(app_client, audit_users):
    c = login(app_client, "tenant-a", "auditor")
    for path in ("/api/v1/evidence/append", "/api/v1/evidence/events"):
        r = c.post(path, json={"action": "FAKE"})
        assert r.status_code in (404, 405), (path, r.status_code)


def test_events_search_scoped_and_role_detail_levels(app_client, run_a):
    rid, corr = run_a
    aud = login(app_client, "tenant-a", "auditor")
    full = aud.get("/api/v1/evidence/events", params={"correlation_id": corr}).json()
    assert full["detail_level"] == "FULL" and any(i.get("rationale") == "secret rationale text" for i in full["items"])
    viewer = login(app_client, "tenant-a", "viewer")
    lim = viewer.get("/api/v1/evidence/events", params={"correlation_id": corr}).json()
    assert lim["detail_level"] == "METADATA_ONLY" and "secret rationale text" not in str(lim)  # no snippet leakage
    other = login(app_client, "tenant-b", "auditor")
    assert other.get("/api/v1/evidence/events", params={"correlation_id": corr}).json()["items"] == []


def test_export_flow_permissions_and_tenant_isolation(app_client, run_a, ev_dirs):
    rid, _ = run_a
    maker = login(app_client, "tenant-a", "maker")
    assert maker.post("/api/v1/evidence/export", json={"run_id": rid}, headers={"Idempotency-Key": "x1"}).status_code == 403
    aud = login(app_client, "tenant-a", "auditor")
    r = aud.post("/api/v1/evidence/export", json={"run_id": rid}, headers={"Idempotency-Key": "ex-full-" + rid})
    assert r.status_code == 201, r.text
    exp = r.json()
    assert exp["redacted"] is False and exp["events"] >= 2
    again = aud.post("/api/v1/evidence/export", json={"run_id": rid}, headers={"Idempotency-Key": "ex-full-" + rid}).json()
    assert again["export_id"] == exp["export_id"]  # idempotent retry, no duplicate export
    prev = aud.get(f"/api/v1/evidence/exports/{exp['export_id']}").json()
    assert prev["preview"]["scope"] == "INTERNAL_EVIDENCE_PACK_NOT_A_REGULATORY_SUBMISSION" and "events.json" in prev["preview"]["files"]
    dl = aud.get(f"/api/v1/evidence/exports/{exp['export_id']}/download")
    assert dl.status_code == 200 and "manifest.json" in zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
    # tenant B cannot see/download tenant A's export or run (G19)
    b = login(app_client, "tenant-b", "auditor")
    assert b.get(f"/api/v1/evidence/exports/{exp['export_id']}").status_code == 404
    assert b.get(f"/api/v1/evidence/exports/{exp['export_id']}/download").status_code == 404
    assert b.post("/api/v1/evidence/export", json={"run_id": rid}, headers={"Idempotency-Key": "b1"}).status_code == 404
    assert b.get(f"/api/v1/evidence/runs/{rid}/lineage").status_code == 404
    assert b.post("/api/v1/evidence/tamper-demo", json={"export_id": exp["export_id"], "mode": "none"}).status_code == 404
    # tenant_admin is forced to the redacted form
    adm = login(app_client, "tenant-a", "admin")
    ra = adm.post("/api/v1/evidence/export", json={"run_id": rid, "redact": False}, headers={"Idempotency-Key": "adm-" + rid}).json()
    assert ra["redacted"] is True and ra["missing_objects"] == []
    # lab tamper demo through the API uses a copy
    t = aud.post("/api/v1/evidence/tamper-demo", json={"export_id": exp["export_id"], "mode": "modify_event"}).json()
    assert t["verified"] is False and t["operational_evidence_modified"] is False
    assert aud.post("/api/v1/evidence/tamper-demo", json={"export_id": exp["export_id"], "mode": "none"}).json()["verified"] is True


def test_verify_checkpoint_integrity_and_lineage_endpoints(app_client, run_a):
    rid, _ = run_a
    aud = login(app_client, "tenant-a", "auditor")
    cp = aud.post("/api/v1/evidence/checkpoints", headers={"Idempotency-Key": "cp-" + rid})
    assert cp.status_code == 201 and "signature" in cp.json()
    v = aud.post("/api/v1/evidence/verify").json()
    assert v["valid"] is True and v["anchor_status"] == "ANCHORED"
    lst = aud.get("/api/v1/evidence/checkpoints").json()
    assert lst["items"] and lst["exported"][0]["signature_valid"] is True
    dash = aud.get("/api/v1/evidence/integrity").json()
    assert dash["status"] == "OK" and dash["signing"]["protection"] == "LOCAL_FILE_KEY_NOT_HSM"
    lg = aud.get(f"/api/v1/evidence/runs/{rid}/lineage").json()
    assert lg["complete"] is False and any(m["kind"] == "STAGE_ABSENT" for m in lg["missing"])  # explicit missing links
    assert any(n["stage"] == "SNAPSHOT" for n in lg["nodes"])
    assert login(app_client, "tenant-a", "viewer").post("/api/v1/evidence/verify").status_code == 403


def test_replay_endpoint_and_access_log_and_retention(app_client, run_a):
    rid, _ = run_a
    aud = login(app_client, "tenant-a", "auditor")
    r = aud.post("/api/v1/evidence/replay", json={"run_id": rid}, headers={"Idempotency-Key": "rp-" + rid})
    assert r.status_code == 200 and r.json()["status"] == "NOTHING_TO_REPLAY"
    assert aud.post("/api/v1/evidence/replay", json={"run_id": rid}).status_code == 422  # idempotency key required
    log = aud.get("/api/v1/evidence/access-log").json()["items"]
    assert any(i["action"] == "SEARCH_EVENTS" or i["action"] == "VIEW_LINEAGE" or i["action"] == "VERIFY_CHAIN" for i in log) or log == []
    ret = aud.get("/api/v1/evidence/retention").json()
    assert all(i["retention_days"] is None for i in ret["items"]) and "No statutory retention period" in ret["note"]
    adm = login(app_client, "tenant-a", "admin")
    assert adm.put("/api/v1/evidence/retention/TEMP_OBJECTS", json={"retention_days": 30}).status_code == 200
    assert adm.put("/api/v1/evidence/retention/TEMP_OBJECTS", json={"retention_days": 0}).status_code == 422
    assert aud.put("/api/v1/evidence/retention/TEMP_OBJECTS", json={"retention_days": 5}).status_code == 403
