"""E-A01 / E-A05 / lineage completeness, end to end with the REAL engines (no fakes), full demo seed.

A6 verifies, for a decision made under ONE run correlation id:
  * every engine (platform, regulatory, contracts, risk, collateral, compliance, evidence) wrote evidence for the seeded flow;
  * the lineage graph contains the stages that flow really produces, and the stages it cannot produce are REPORTED missing
    (never papered over): G01 ETD has no bilateral SOURCE/APPROVAL stage, so lineage is honestly incomplete;
  * read-only replay MATCHes for every engine that already emits `payload.replay`, and `NOT_REPLAYABLE` is reported (not hidden)
    for any replay spec whose replayer is not registered;
  * the chain verifies and an anchored export verifies with the offline verifier.
Expected values (12,500,000 INR; 9,500,000 USD) are the spec's hand-derived golden values, not outputs of the code under test."""
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core import services
from app.db.base import SessionLocal
from app.db.models_core import Run
from app.engines.evidence import checkpoints as cps
from app.engines.evidence import keys
from app.engines.evidence import service as ev
from app.engines.evidence.bundle import build_bundle
from app.engines.evidence.lineage import build_lineage
from app.engines.evidence.models import EvEvent
from app.engines.evidence.objects import read_object
from app.engines.evidence.replay import replay_run

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "scripts"))
from seed_demo import seed_engines  # noqa: E402

AS_OF = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
D = Decimal


def _run(s, tenant, scenario, market, corr):
    rid = str(uuid.uuid4())
    s.add(Run(run_id=rid, tenant_id=tenant, scenario_id=scenario, market=market, as_of=AS_OF, correlation_id=corr, created_by="e2e-maker"))
    s.flush()
    services.call("evidence.append", s, tenant, engine="platform", action="RUN_CREATED", actor_id="e2e-maker", actor_type="USER",
                  entity_type="run", entity_id=rid, correlation_id=corr, effective_at=AS_OF, payload={"scenario_id": scenario, "market": market})
    return rid


@pytest.fixture(scope="module")
def flow():
    tenant = "e2e-" + uuid.uuid4().hex[:8]
    seed_engines(tenant)
    out = {"tenant": tenant}
    now = datetime.now(UTC)
    with SessionLocal() as s:
        # ---- G01: domestic ETD, hypothetical rule revision -> shortfall -> case -> collateral proposal
        corr1 = "corr-g01-" + uuid.uuid4().hex[:6]
        out["g01_run"], out["g01_corr"] = _run(s, tenant, "ETD-DOM-G01", "ETD_DOMESTIC", corr1), corr1
        from app.engines.regulatory.models import RegRule
        v2 = s.execute(select(RegRule).where(RegRule.tenant_id == tenant, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one()
        ctx = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": "ACC-DEMO-NSE-001", "currency": "INR",
               "as_of": AS_OF.isoformat(), "correlation_id": corr1}
        rules = services.call("regulatory.resolve_rules", s, tenant, ctx, AS_OF, None, [v2.rule_id])
        margin = services.call("risk.compute_margin", s, tenant, ctx, "SNAP-ETD-001", None, rules["rule_set_hash"], "ETD_IMPORTED_V1")
        out["g01_margin"] = margin
        out["g01_compliance"] = services.call("compliance.evaluate", s, tenant, ctx, "SNAP-ETD-001", rules["rule_set_hash"],
                                              margin["result_id"], correlation_id=corr1)
        out["g01_proposal"] = services.call("collateral.optimise", s, tenant, margin["result_id"], "INV-ETD-001", None, None,
                                            "2026-08-19T04:00:00Z", 30, actor_id="e2e", actor_type="SERVICE", correlation_id=corr1)
        # ---- G03: OTC rate shock under frozen terms
        corr3 = "corr-g03-" + uuid.uuid4().hex[:6]
        out["g03_run"], out["g03_corr"] = _run(s, tenant, "OTC-SHOCK-G03", "OTC", corr3), corr3
        terms = services.call("contracts.resolve_terms", s, tenant, "NS-MER-NWD-001", AS_OF, now)
        out["g03_terms"] = terms
        out["g03_margin"] = services.call("risk.compute_margin", s, tenant, {
            "market_type": "OTC", "netting_set_id": "NS-MER-NWD-001", "as_of": AS_OF.isoformat(), "known_at": now.isoformat(),
            "existing_vm_balance": {"PARTY_A": "0", "PARTY_B": "0"}, "correlation_id": corr3},
            "SNAP-SHOCK-001", terms["terms_hash"], None, "OTC_VM_SIMPLE_V1")
        s.commit()
    return out


def test_golden_values_through_the_real_engines(flow):
    assert flow["g01_margin"]["status"] == "OK" and D(flow["g01_margin"]["required_amount"]["amount"]) == D("12500000")
    assert flow["g03_margin"]["status"] == "OK" and D(flow["g03_margin"]["rounded_amount"]["amount"]) == D("9500000")


def test_e_a01_every_engine_wrote_evidence_for_the_seeded_flow(flow):
    t = flow["tenant"]
    with SessionLocal() as s:
        # evidence engine's own events appear once an auditor-style action happens (replay below records REPLAY_COMPLETED)
        replay_run(s, t, flow["g01_run"], flow["g01_corr"], "auditor-e2e")
        s.commit()
        engines = {e for (e,) in s.execute(select(EvEvent.engine).where(EvEvent.tenant_id == t).distinct())}
        assert {"platform", "regulatory", "contracts", "risk", "collateral", "compliance", "evidence"} <= engines, engines
        # the decision's own correlation carries events from the engines that took part in it
        g01 = {(e, a) for e, a in s.execute(select(EvEvent.engine, EvEvent.action).where(EvEvent.tenant_id == t, EvEvent.correlation_id == flow["g01_corr"]))}
        got = ev.check_expected_actions(s, t, flow["g01_corr"], {"platform": ["RUN_CREATED"], "risk": ["MARGIN_COMPUTED"],
                                                                 "compliance": ["CONTROL_EVALUATED", "CASE_OPENED"],
                                                                 "collateral": ["COLLATERAL_PROPOSAL_CREATED"]})
        assert got["complete"], (got["missing"], sorted(g01))
        assert ev.verify_chain(s, t)["valid"]


def test_lineage_for_g01_has_the_real_stages_and_reports_the_rest_as_missing(flow):
    with SessionLocal() as s:
        g = build_lineage(s, flow["tenant"], flow["g01_corr"])
    stages = {n["stage"] for n in g["nodes"] if n["recorded"]}
    assert {"RULE_TERMS", "SNAPSHOT", "MARGIN", "PROPOSAL", "CONTROL"} <= stages, stages
    types = {n["entity_type"] for n in g["nodes"] if n["recorded"]}
    assert "risk_result" in types and "margin_result" not in {n["entity_type"] for n in g["nodes"]}    # canonical identity
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    mid = flow["g01_margin"]["result_id"]
    # margin -> proposal and margin -> control edges exist (parents are honoured across the alias margin_result/risk_result)
    assert any(a == f"risk_result:{mid}" and b.startswith("allocation_proposal:") for a, b in edges)
    assert any(a == f"risk_result:{mid}" and b.startswith("control_evaluation:") for a, b in edges)
    # seed-time snapshots/rules are found ACROSS correlations (external) rather than reported as missing
    ext = {n["entity_type"] for n in g["nodes"] if n.get("external") and n["recorded"]}
    assert {"risk_snapshot", "reg_rule", "inventory_snapshot"} <= ext, ext
    # honesty: ETD flow has no bilateral SOURCE terms and no approval, so lineage must NOT claim completeness
    absent = {m["stage"] for m in g["missing"] if m["kind"] == "STAGE_ABSENT"}
    assert {"APPROVAL", "METHOD"} <= absent and g["complete"] is False
    print("G01 lineage missing:", [(m["kind"], m.get("stage") or m.get("node_id")) for m in g["missing"]])


def test_lineage_for_g03_otc_links_terms_snapshot_and_margin(flow):
    with SessionLocal() as s:
        g = build_lineage(s, flow["tenant"], flow["g03_corr"])
    stages = {n["stage"] for n in g["nodes"] if n["recorded"]}
    assert {"SNAPSHOT", "MARGIN"} <= stages, stages
    mid = flow["g03_margin"]["result_id"]
    assert any(n["node_id"] == f"risk_result:{mid}" and n["recorded"] for n in g["nodes"])
    print("G03 lineage stages:", sorted(stages), "missing:", [(m["kind"], m.get("stage") or m.get("node_id")) for m in g["missing"]])


@pytest.mark.parametrize("which", ["g01", "g03"])
def test_replay_matches_for_every_engine_that_emits_payload_replay(flow, which):
    """E-A05. Engines that already emit payload.replay must MATCH; a spec whose replayer is absent must be NOT_REPLAYABLE (reported)."""
    with SessionLocal() as s:
        corr = flow[f"{which}_corr"]
        specs = [(e.engine, e.action, e.payload["replay"]) for e in s.execute(select(EvEvent).where(
            EvEvent.tenant_id == flow["tenant"], EvEvent.correlation_id == corr).order_by(EvEvent.sequence)).scalars() if (e.payload or {}).get("replay")]
        before = s.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == flow["tenant"])).scalar_one()
        r = replay_run(s, flow["tenant"], flow[f"{which}_run"], corr, "auditor-e2e")
        s.flush()
        after = s.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == flow["tenant"])).scalar_one()
        s.commit()
    assert after - before == 1                                       # replay is read-only: it adds ONLY its own REPLAY_COMPLETED event
    assert len(r["items"]) == len(specs)
    registered = {sp["replayer"]: services.is_registered(sp["replayer"]) for _, _, sp in specs}
    for it, (eng, action, spec) in zip(r["items"], specs, strict=True):
        if registered[spec["replayer"]]:
            assert it["status"] in ("MATCH", "MATCH_WITHIN_TOLERANCE"), (eng, action, it)
        else:
            assert it["status"] == "NOT_REPLAYABLE" and "REPLAYER_NOT_AVAILABLE" in it["reason"], (eng, action, it)
    covered = {eng for eng, _, spec in specs if registered[spec["replayer"]]}
    print(f"{which}: replayable engines with MATCH:", sorted(covered), "| not replayable:", sorted({e for e, _, sp in specs if not registered[sp['replayer']]}))
    assert "risk" in covered                                         # VECTOR is the sole margin producer and must be replayable


def test_missing_replayer_is_reported_not_hidden_for_a_unregistered_engine(flow):
    """If an engine had emitted a replay spec but its replayer is absent, status is NOT_REPLAYABLE (never a silent skip)."""
    t = flow["tenant"]
    corr = "corr-nr-" + uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        rid = _run(s, t, "X", "OTC", corr)
        ev.append(s, t, engine="regulatory", action="IMPACT_RUN_COMPLETED", actor_id="u", actor_type="SERVICE", entity_type="reg_impact_run",
                  entity_id="I1", correlation_id=corr,
                  payload={"replay": {"replayer": "replay.not-registered", "engine_version": "1", "method_version": "1", "business_hash": "00"}})
        r = replay_run(s, t, rid, corr, "auditor-e2e")
        s.commit()
    assert r["status"] == "NOT_REPLAYABLE" and r["items"][0]["reason"].startswith("REPLAYER_NOT_AVAILABLE")


def test_anchored_export_of_the_decision_verifies_offline_and_tamper_fails(flow, tmp_path):
    t = flow["tenant"]
    with SessionLocal() as s:
        out = build_bundle(s, t, flow["g01_run"], flow["g01_corr"], "auditor-e2e")
        s.commit()
        data = read_object(s, t, out["bundle_hash"])
    b = tmp_path / "g01.zip"
    b.write_bytes(data)
    pub = tmp_path / "trusted.pub"
    pub.write_text(next(k for k in keys.public_keys() if k["current"])["public_key_pem"])
    cmd = [sys.executable, "-m", "tools.verify_bundle", str(b), "--trusted-key", str(pub)]
    env = {"PYTHONPATH": str(ROOT), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows")}
    assert subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env).returncode == 0
    raw = bytearray(data)
    raw[len(raw) // 2] ^= 0xFF
    bad = tmp_path / "g01-bad.zip"
    bad.write_bytes(bytes(raw))
    assert subprocess.run([*cmd[:3], str(bad), *cmd[4:]], cwd=ROOT, capture_output=True, text=True, env=env).returncode != 0
    with SessionLocal() as s:
        assert cps.verify_against_exported(s, t)["valid"]
