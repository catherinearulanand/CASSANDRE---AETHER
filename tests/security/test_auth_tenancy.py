import pytest
from fastapi.testclient import TestClient

from tests.conftest import login

pytestmark = pytest.mark.security
H = {"Idempotency-Key": "k"}


def test_unauthenticated_rejected_with_envelope(app_client):
    r = app_client.get("/api/v1/runs")
    assert r.status_code == 401
    assert set(r.json()) == {"code", "message", "field_errors", "correlation_id", "retryable"}


def test_bad_password_and_rate_limit(app_client):
    c = TestClient(app_client.app)
    codes = [c.post("/api/v1/session/login", json={"tenant_id": "tenant-a", "username": "ghost", "password": "bad"}).status_code
             for _ in range(7)]
    assert codes[0] == 401 and codes[-1] == 429


def test_csrf_required_on_mutation(app_client):
    c = login(app_client, "tenant-a", "maker")
    tok = c.headers.pop("x-csrf-token")
    assert c.post("/api/v1/runs", json={"scenario_id": "x"}, headers=H).status_code == 403
    c.headers["x-csrf-token"] = tok


def test_viewer_cannot_create_run(app_client):
    c = login(app_client, "tenant-a", "viewer")
    assert c.post("/api/v1/runs", json={"scenario_id": "ETD-DOM-G01"}, headers=H).status_code == 403


def test_tenant_isolation_and_idempotency(app_client):
    a = login(app_client, "tenant-a", "maker")
    b = login(app_client, "tenant-b", "maker")
    r1 = a.post("/api/v1/runs", json={"scenario_id": "ETD-DOM-G01"}, headers={"Idempotency-Key": "k1"})
    assert r1.status_code == 201, r1.text
    r2 = a.post("/api/v1/runs", json={"scenario_id": "ETD-DOM-G01"}, headers={"Idempotency-Key": "k1"})
    assert r2.json()["run_id"] == r1.json()["run_id"]  # G16: same key, same run
    r3 = a.post("/api/v1/runs", json={"scenario_id": "OTC-SHOCK-G03"}, headers={"Idempotency-Key": "k1"})
    assert r3.status_code == 409  # G16: different payload, same key
    rid = r1.json()["run_id"]
    assert a.get(f"/api/v1/runs/{rid}").status_code == 200
    assert b.get(f"/api/v1/runs/{rid}").status_code == 404  # G19: no leakage
    assert b.get(f"/api/v1/runs/{rid}/events").status_code == 404
    assert rid not in [x["run_id"] for x in b.get("/api/v1/runs").json()["items"]]


def test_run_creation_wrote_evidence_atomically(app_client):
    a = login(app_client, "tenant-a", "maker")
    rid = a.post("/api/v1/runs", json={"scenario_id": "OTC-SHOCK-G03"}, headers={"Idempotency-Key": "k-ev"}).json()["run_id"]
    ev = a.get(f"/api/v1/runs/{rid}/events").json()["items"]
    assert [e["action"] for e in ev] == ["RUN_CREATED"]


def test_capabilities_honest(app_client):
    c = login(app_client, "tenant-a", "viewer")
    j = c.get("/api/v1/capabilities").json()
    assert j["external_transmission"] == "DISABLED" and len(j["engines"]) == 6
