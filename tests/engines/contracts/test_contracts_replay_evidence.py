"""Evidence contract v1.1 for COVENANT: payload.parents lineage edges and payload.replay specs, replayed through the
real VERITAS replay service (read-only). Covers MATCH, historical reproducibility after a later amendment (C-A08),
tamper -> DIVERGED, and NOT_REPLAYABLE (missing/mismatched method version, unregistered replayer)."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.core import services
from app.engines.contracts import fixtures as F
from app.engines.contracts import services as S
from app.engines.contracts.models import ConVersion
from app.engines.evidence.lineage import build_lineage
from app.engines.evidence.models import EvEvent
from tests.engines.contracts.conftest import new_pkg, principal

AS_OF = datetime(2026, 8, 17, 4, tzinfo=UTC)


def replay(db, tenant, corr):
    return services.call("evidence.replay_run", db, tenant, "run-" + corr, corr, "u-auditor")


def frozen(db, tenant, maker, checker, corr, ns="NS-DEMO-G25"):
    pkg = new_pkg(db, maker, ns, corr=corr)
    pid = pkg["package_id"]
    S.extract(db, maker, corr, pid, "RULES")
    F._approve_all(db, checker, corr, pid)
    v = F._freeze(db, maker, checker, corr, pid)
    db.commit()
    return pid, v


def ev_of(db, tenant, corr, action):
    return db.execute(select(EvEvent).where(EvEvent.tenant_id == tenant, EvEvent.correlation_id == corr,
                                            EvEvent.action == action)).scalars().all()


def test_frozen_event_carries_parents_and_replay_spec_and_replay_matches(db, tenant, maker, checker):
    corr = uuid.uuid4().hex
    pid, v = frozen(db, tenant, maker, checker, corr)
    (fz,) = ev_of(db, tenant, corr, "TERMS_FROZEN")
    spec = fz.payload["replay"]
    assert spec["replayer"] == "replay.contracts" and spec["engine_version"] == S.ENGINE_VERSION and spec["method_version"] == S.METHOD_VERSION
    assert spec["business_hash"] == v["bundle_hash"] and spec["inputs"] == {"kind": "FROZEN_BUNDLE", "version_id": v["version_id"]}
    kinds = {p["entity_type"] for p in fz.payload["parents"]}
    assert kinds == {"contract_field", "approval"} and len(fz.payload["parents"]) >= 6
    r = replay(db, tenant, corr)
    assert r["status"] == "MATCH" and r["read_only"] is True, r
    assert any(i["action"] == "TERMS_FROZEN" and i["status"] == "MATCH" for i in r["items"])


def test_lineage_runs_document_to_field_to_approval_to_terms(db, tenant, maker, checker):
    corr = uuid.uuid4().hex
    frozen(db, tenant, maker, checker, corr)
    g = build_lineage(db, tenant, corr)
    nodes = {n["node_id"]: n for n in g["nodes"]}
    assert any(n["entity_type"] == "contract_document" and n["recorded"] for n in nodes.values())
    assert any(n["entity_type"] == "approval" and n["recorded"] for n in nodes.values())
    assert any(n["entity_type"] == "contract_terms" and n["recorded"] for n in nodes.values())
    edge_types = {(e["from"].split(":")[0], e["to"].split(":")[0]) for e in g["edges"]}
    assert ("contract_document", "contract_field") in edge_types
    assert ("contract_field", "approval") in edge_types and ("approval", "contract_terms") in edge_types
    # nothing the contracts engine references is left dangling in its own correlation
    assert not [m for m in g["missing"] if m["kind"] == "PARENT_NOT_RECORDED"], g["missing"]


def test_audited_resolution_replays_and_stays_reproducible_after_later_amendment(db, tenant, maker, checker):
    corr = uuid.uuid4().hex
    pkg = F.seed_fixtures(db, tenant)["packages"]["NS-MER-NWD-001"]  # v1 (10m) then v2 (8m) from 2026-09-01
    db.commit()
    known_before_v2 = datetime.fromisoformat(
        db.execute(select(ConVersion.frozen_at).where(ConVersion.package_id == pkg["package_id"], ConVersion.version_no == 1)).scalar_one().isoformat())
    known_before_v2 = known_before_v2.replace(microsecond=known_before_v2.microsecond + 1)  # strictly after v1 was frozen
    chk = principal(tenant, "u-checker", "checker")
    r1 = S.record_resolution(db, chk, corr, "NS-MER-NWD-001", AS_OF, known_before_v2, None)
    db.commit()
    assert r1["status"] == "RESOLVED" and r1["terms"]["threshold_a"] in ("10000000", "10000000.00") and r1["version_no"] == 1
    (re,) = ev_of(db, tenant, corr, "TERMS_RESOLVED")
    assert re.payload["replay"]["business_hash"] == r1["terms_hash"] and re.entity_id == r1["terms_hash"]
    # v2 is already frozen (seed): replay must still reproduce the v1 resolution because known_at pins the version (C-A08)
    out = replay(db, tenant, corr)
    assert out["status"] == "MATCH", out
    now_res = S.resolve_terms(db, tenant, "NS-MER-NWD-001", datetime(2026, 9, 2, tzinfo=UTC), None)
    assert now_res["version_no"] == 2 and now_res["terms_hash"] != r1["terms_hash"]  # the live answer differs; the replay did not drift


def test_blocked_resolution_is_audited_and_replays_as_the_same_block(db, tenant, seeded):
    corr = uuid.uuid4().hex
    chk = principal(tenant, "u-checker", "checker")
    r = S.record_resolution(db, chk, corr, "NS-DEMO-G14-RATING", AS_OF, None, None)  # unresolved rating trigger (G14)
    db.commit()
    assert r["status"] == "BLOCKED" and r["blocking_reasons"]
    (ev,) = ev_of(db, tenant, corr, "TERMS_RESOLUTION_BLOCKED")
    assert ev.payload["blocking_reasons"] == r["blocking_reasons"]
    assert replay(db, tenant, corr)["status"] == "MATCH"


def test_post_freeze_tampering_is_detected_by_replay(db, tenant, maker, checker):
    corr = uuid.uuid4().hex
    pid, v = frozen(db, tenant, maker, checker, corr)
    ver = db.get(ConVersion, v["version_id"])
    ents = [dict(e) for e in ver.entries]
    thr = next(e for e in ents if e["key"] == "threshold_a")
    thr["value"] = "99999999"  # an attacker with DB write access silently raises a threshold
    ver.entries = ents
    flag_modified(ver, "entries")
    db.commit()
    r = replay(db, tenant, corr)
    assert r["status"] == "DIVERGED" and r["first_divergence"]["first_divergence"] == "business_hash", r


def test_not_replayable_when_versions_missing_or_mismatched_or_replayer_absent(db, tenant, maker, checker):
    corr = uuid.uuid4().hex
    pid, v = frozen(db, tenant, maker, checker, corr)
    (fz,) = ev_of(db, tenant, corr, "TERMS_FROZEN")
    base = dict(fz.payload["replay"])

    def emit(c, spec):
        services.call("evidence.append", db, tenant, engine="contracts", action="TERMS_FROZEN", actor_id="u", actor_type="USER",
                      entity_type="contract_terms", entity_id=pid, correlation_id=c, payload={"replay": spec})
        db.commit()

    cases = {"missing": {**base, "method_version": None}, "mismatch": {**base, "method_version": "terms-resolution/0"},
             "absent": {**base, "replayer": "replay.contracts_v0"}}
    expected = {"missing": "MISSING_METHOD_VERSION", "mismatch": "VERSION_MISMATCH", "absent": "REPLAYER_NOT_AVAILABLE"}
    for name, spec in cases.items():
        c = f"{corr}-{name}"
        emit(c, spec)
        r = replay(db, tenant, c)
        assert r["status"] == "NOT_REPLAYABLE", (name, r)
        assert expected[name] in r["items"][0]["reason"], (name, r["items"][0])
