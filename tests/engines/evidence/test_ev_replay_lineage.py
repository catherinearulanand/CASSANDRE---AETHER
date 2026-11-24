"""E-A05 / E-A06 replay, lineage with explicit missing links, E-A01 helper, G24 evidence outage."""
import pytest

from app.core import services
from app.core.errors import Unavailable
from app.db.base import SessionLocal
from app.engines.evidence import lineage as lin
from app.engines.evidence import service as ev
from app.engines.evidence.models import EvAccessLog
from app.engines.evidence.replay import replay_run


def _replayable(s, tenant, corr, spec, **kw):
    return ev.append(s, tenant, engine="risk", action="MARGIN_COMPUTED", actor_id="u", actor_type="SERVICE", entity_type="margin_result",
                     entity_id=kw.get("eid", "MR-1"), correlation_id=corr, payload={"replay": spec})


SPEC = {"replayer": "replay.test", "engine_version": "risk/1.0", "method_version": "ois-1", "business_hash": "h" * 64, "inputs": {}}


@pytest.fixture
def replayer():
    calls = {}

    def fn(session, tenant_id, spec):
        calls["n"] = calls.get("n", 0) + 1
        session.add(EvAccessLog(tenant_id=tenant_id, actor_id="REPLAY-WRITE", action="SHOULD_BE_ROLLED_BACK"))  # a write attempt
        session.flush()
        return {"engine_version": calls.get("ev", "risk/1.0"), "method_version": calls.get("mv", "ois-1"),
                "business_hash": calls.get("hash", "h" * 64), "values": calls.get("values")}
    services.register("replay.test", fn)
    yield calls
    services._REGISTRY.pop("replay.test", None)


def test_replay_match_and_is_read_only(tenant, replayer):
    with SessionLocal() as s:
        _replayable(s, tenant, "c1", SPEC)
        s.commit()
        out = replay_run(s, tenant, "run-1", "c1", "auditor")
        s.commit()
        assert out["status"] == "MATCH" and out["read_only"]
        from sqlalchemy import select
        assert s.execute(select(EvAccessLog).where(EvAccessLog.actor_id == "REPLAY-WRITE")).first() is None  # rolled back
        actions = [r.action for r in s.execute(select(ev.EvEvent).where(ev.EvEvent.tenant_id == tenant).order_by(ev.EvEvent.sequence)).scalars()]
        assert actions == ["MARGIN_COMPUTED", "REPLAY_COMPLETED"]  # replay recorded separately


def test_replay_divergence_reports_first_divergence(tenant, replayer):
    replayer["hash"] = "x" * 64
    with SessionLocal() as s:
        _replayable(s, tenant, "c2", SPEC)
        s.commit()
        out = replay_run(s, tenant, "run-2", "c2", "auditor")
        assert out["status"] == "DIVERGED" and out["first_divergence"]["first_divergence"] == "business_hash"


def test_replay_within_declared_tolerance(tenant, replayer):
    replayer["hash"] = "x" * 64
    replayer["values"] = {"required": "9500000.004"}
    spec = {**SPEC, "tolerance": "0.01", "values": {"required": "9500000.000"}}
    with SessionLocal() as s:
        _replayable(s, tenant, "c3", spec)
        s.commit()
        assert replay_run(s, tenant, "run-3", "c3", "a")["status"] == "MATCH_WITHIN_TOLERANCE"
    replayer["values"] = {"required": "9500001.000"}
    with SessionLocal() as s:
        _replayable(s, tenant, "c3b", spec)
        s.commit()
        assert replay_run(s, tenant, "run-3b", "c3b", "a")["status"] == "DIVERGED"


def test_missing_or_drifted_method_version_is_not_replayable(tenant, replayer):
    """E-A06: never silently use the current version."""
    with SessionLocal() as s:
        _replayable(s, tenant, "c4", {k: v for k, v in SPEC.items() if k != "method_version"})
        _replayable(s, tenant, "c5", SPEC)
        _replayable(s, tenant, "c6", {**SPEC, "replayer": "replay.absent"})
        s.commit()
        assert replay_run(s, tenant, "r4", "c4", "a")["items"][0]["reason"] == "MISSING_METHOD_VERSION"
        replayer["mv"] = "ois-2"
        o5 = replay_run(s, tenant, "r5", "c5", "a")
        assert o5["status"] == "NOT_REPLAYABLE" and o5["items"][0]["reason"] == "VERSION_MISMATCH"
        assert replay_run(s, tenant, "r6", "c6", "a")["items"][0]["reason"].startswith("REPLAYER_NOT_AVAILABLE")


def test_nothing_to_replay(tenant):
    with SessionLocal() as s:
        ev.append(s, tenant, engine="platform", action="RUN_CREATED", actor_id="u", actor_type="USER", entity_type="run",
                  entity_id="r", correlation_id="c7")
        assert replay_run(s, tenant, "r7", "c7", "a")["status"] == "NOTHING_TO_REPLAY"


def _stage_event(s, tenant, corr, etype, eid, parents=(), spans=None):
    ev.append(s, tenant, engine="x", action="STEP", actor_id="u", actor_type="SERVICE", entity_type=etype, entity_id=eid,
              correlation_id=corr, payload={"parents": [{"entity_type": t, "entity_id": i} for t, i in parents]}, source_spans=spans)


def test_lineage_complete_chain_and_ordering(tenant):
    c = "lin1"
    with SessionLocal() as s:
        _stage_event(s, tenant, c, "rule_version", "R1", spans=[{"document_id": "DOC1", "start": 1, "end": 9}])
        _stage_event(s, tenant, c, "snapshot", "SNAP-1")
        _stage_event(s, tenant, c, "method", "ois-1")
        _stage_event(s, tenant, c, "margin_result", "MR1", parents=[("rule_version", "R1"), ("snapshot", "SNAP-1"), ("method", "ois-1")])
        _stage_event(s, tenant, c, "allocation_proposal", "P1", parents=[("margin_result", "MR1")])
        _stage_event(s, tenant, c, "control_result", "C1", parents=[("allocation_proposal", "P1")])
        _stage_event(s, tenant, c, "approval", "A1", parents=[("allocation_proposal", "P1")])
        g = lin.build_lineage(s, tenant, c)
    assert g["complete"] and g["missing"] == []
    stages = [n["stage"] for n in g["nodes"]]
    assert stages == sorted(stages, key=lin.STAGES.index)
    assert {"from": "snapshot:SNAP-1", "to": "margin_result:MR1"} in g["edges"]
    assert {"from": "source_span:DOC1:1-9", "to": "rule_version:R1"} in g["edges"]


def test_lineage_reports_missing_links_explicitly(tenant):
    c = "lin2"
    with SessionLocal() as s:
        _stage_event(s, tenant, c, "margin_result", "MR9", parents=[("snapshot", "SNAP-GONE")])
        g = lin.build_lineage(s, tenant, c)
    assert not g["complete"]
    assert any(m["kind"] == "PARENT_NOT_RECORDED" and m["node_id"] == "snapshot:SNAP-GONE" for m in g["missing"])
    stages_absent = {m["stage"] for m in g["missing"] if m["kind"] == "STAGE_ABSENT"}
    assert {"SOURCE", "APPROVAL"} <= stages_absent


def test_expected_actions_helper(tenant):
    with SessionLocal() as s:
        ev.append(s, tenant, engine="risk", action="MARGIN_COMPUTED", actor_id="u", actor_type="SERVICE", entity_type="m", entity_id="1", correlation_id="h1")
        r = services.call("evidence.check_expected_actions", s, tenant, "h1",
                          {"risk": ["MARGIN_COMPUTED"], "collateral": ["ALLOCATION_PROPOSED"]})
    assert not r["complete"] and r["missing"] == [{"engine": "collateral", "action": "ALLOCATION_PROPOSED"}]


def test_outage_blocks_append_and_material_transition_g24(app_client, tenant):
    """G24: evidence unavailable during a material transition -> transition is rolled back; recovery retry works."""
    from tests.conftest import login
    a = login(app_client, "tenant-a", "maker")
    rid = a.post("/api/v1/runs", json={"scenario_id": "ETD-DOM-G01"}, headers={"Idempotency-Key": "g24-create"}).json()["run_id"]
    ev.set_storage_outage(True)
    try:
        r = a.post(f"/api/v1/runs/{rid}/cancel")
        assert r.status_code == 503 and r.json()["code"] == "EVIDENCE_UNAVAILABLE" and r.json()["retryable"] is True
        assert a.get(f"/api/v1/runs/{rid}").json()["state"] == "CREATED"  # no state change without evidence
        with SessionLocal() as s, pytest.raises(Unavailable):
            ev.append(s, tenant, engine="x", action="A", actor_id="u", actor_type="USER", entity_type="x", entity_id="1", correlation_id="c")
    finally:
        ev.set_storage_outage(False)
    assert a.post(f"/api/v1/runs/{rid}/cancel").status_code == 200  # idempotent recovery
    assert [e["action"] for e in a.get(f"/api/v1/runs/{rid}/events").json()["items"]] == ["RUN_CREATED", "RUN_CANCELLED"]
