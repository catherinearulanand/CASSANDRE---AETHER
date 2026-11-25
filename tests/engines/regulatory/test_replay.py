"""VERITAS contract v1.1 for REGULUS: payload.parents lineage + payload.replay / replay.regulatory (read-only, business hash)."""
from sqlalchemy import func, select, text

from app.core import services
from app.engines.evidence import lineage
from app.engines.evidence.models import EvEvent
from app.engines.regulatory import impact, registry
from app.engines.regulatory.fixtures import seed_fixtures
from app.engines.regulatory.models import RegRuleSet
from tests.engines.regulatory.helpers import dt, fake_services
from tests.engines.regulatory.test_impact import G, body, coverage, fake_risk

CORR = "corr-replay-1"


def replay(env, corr=CORR):
    return services.call("evidence.replay_run", env.db, env.tenant, "run-1", corr, "auditor-1")


def make_active(env, corr=CORR):
    r = registry.create_rule(env.db, env.maker, env.body(), corr=corr)
    registry.submit(env.db, env.maker, r.rule_id, "ready", None, corr=corr)
    registry.review(env.db, env.checker, r.rule_id, "APPROVE", "ok", None, corr=corr)
    return registry.activate(env.db, env.checker, r.rule_id, None, "go", as_of=dt("2026-08-01T00:00:00Z"),
                             recorded_at=dt("2026-01-15T00:00:00Z"), corr=corr)


def events(env, corr=CORR):
    return env.db.execute(select(EvEvent).where(EvEvent.tenant_id == env.tenant, EvEvent.correlation_id == corr)
                          .order_by(EvEvent.sequence)).scalars().all()


def test_approved_and_activated_events_carry_replay_spec_and_parents(env):
    make_active(env)
    by = {e.action: e for e in events(env)}
    for action in ("RULE_APPROVED", "RULE_ACTIVATED"):
        p = by[action].payload
        spec = p["replay"]
        assert spec["replayer"] == "replay.regulatory" and spec["engine_version"] and spec["method_version"] and len(spec["business_hash"]) == 64
        assert p["parents"] == [{"entity_type": "source_span", "entity_id": env.span.span_id}]
        assert by[action].source_spans[0]["document_id"] == env.src.source_id  # lineage builds source -> span -> rule from this
    assert "replay" not in by["RULE_DRAFTED"].payload and by["RULE_DRAFTED"].payload["parents"]


def test_replay_match_for_unchanged_rule(env):
    make_active(env)
    out = replay(env)
    assert out["status"] == "MATCH", out
    assert [i["action"] for i in out["items"]] == ["RULE_APPROVED", "RULE_ACTIVATED"] and all(i["status"] == "MATCH" for i in out["items"])
    assert out["read_only"] is True


def test_replay_diverges_when_rule_content_is_tampered(env):
    r = make_active(env)
    env.db.execute(text("UPDATE reg_rules SET parameters = :p WHERE rule_id = :i"),
                   {"p": '{"mult": {"value": "9.99", "unit": "RATIO"}}', "i": r.rule_id})
    env.db.expire_all()
    out = replay(env)
    assert out["status"] == "DIVERGED" and out["first_divergence"]["first_divergence"] == "business_hash"


def test_replay_diverges_when_quoted_source_text_changes(env):
    make_active(env)
    env.db.execute(text("UPDATE reg_sources SET text = replace(text, 'broker', 'BROKER') WHERE source_id = :s"), {"s": env.src.source_id})
    env.db.expire_all()
    assert replay(env)["status"] == "DIVERGED"


def test_replay_not_replayable_when_rule_row_missing_or_replayer_absent(env):
    r = make_active(env)
    with fake_services(**{"replay.regulatory": None}):
        out = replay(env)
    assert out["status"] == "NOT_REPLAYABLE" and "REPLAYER_NOT_AVAILABLE" in out["first_divergence"]["reason"]
    env.db.execute(text("DELETE FROM reg_rule_tests WHERE rule_id = :i"), {"i": r.rule_id})
    env.db.execute(text("DELETE FROM reg_rules WHERE rule_id = :i"), {"i": r.rule_id})
    env.db.expire_all()
    out = replay(env)
    assert out["status"] == "NOT_REPLAYABLE" and "REPLAYER_ERROR" in out["first_divergence"]["reason"]


def test_replay_is_read_only(env):
    seed_fixtures(env.db, env.tenant)
    results = {}

    def get_result(db, tenant, rid):
        return results[rid]

    def risk(db, tenant, ctx, snap, th, rsh, method):
        out = fake_risk(db, tenant, ctx, snap, th, rsh, method)
        out["result_hash"] = "h-" + rsh[:8]
        results[out["result_id"]] = out
        return out

    with fake_services(**{"risk.compute_margin": risk, "risk.get_result": get_result, "collateral.coverage": coverage}):
        run = impact.run_impact(env.db, env.maker, body(env), corr="corr-impact")
        env.db.commit()
        before = env.db.execute(select(func.count()).select_from(RegRuleSet)).scalar_one()
        evs_before = env.db.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == env.tenant)).scalar_one()
        out = services.call("evidence.replay_run", env.db, env.tenant, "run-i", "corr-impact", "auditor-1")
        assert out["status"] == "MATCH", out  # same rule sets re-resolved + same stored risk results re-read
        assert env.db.execute(select(func.count()).select_from(RegRuleSet)).scalar_one() == before  # no writes survive replay
        # only the replay's own evidence (REPLAY_COMPLETED) was added
        assert env.db.execute(select(func.count()).select_from(EvEvent).where(EvEvent.tenant_id == env.tenant)).scalar_one() == evs_before + 1
        # a different stored risk result => DIVERGED, with first divergence reported
        rid = run.results["rows"][0]["hypothetical_result_id"]
        results[rid] = results[rid] | {"result_hash": "tampered", "required_amount": {"amount": "99", "currency": "INR"}}
        assert services.call("evidence.replay_run", env.db, env.tenant, "run-i", "corr-impact", "auditor-1")["status"] == "DIVERGED"
        # the risk result is gone => NOT_REPLAYABLE, never silently skipped
        del results[rid]
        assert services.call("evidence.replay_run", env.db, env.tenant, "run-i", "corr-impact", "auditor-1")["status"] == "NOT_REPLAYABLE"


def test_lineage_has_source_span_rule_and_approval_stages_without_missing_parents(env):
    make_active(env)
    g = lineage.build_lineage(env.db, env.tenant, CORR)
    stages = {n["stage"] for n in g["nodes"] if n["recorded"]}
    assert {"SOURCE", "SPAN", "RULE_TERMS", "APPROVAL"} <= stages
    assert not [m for m in g["missing"] if m["kind"] == "PARENT_NOT_RECORDED" and m["stage"] in ("SOURCE", "SPAN", "RULE_TERMS")]
    edge = {(e["from"], e["to"]) for e in g["edges"]}
    assert (f"source_span:{env.span.span_id}", f"reg_rule:{[n for n in g['nodes'] if n['entity_type'] == 'reg_rule'][0]['entity_id']}") in edge
