"""Concurrent review of the same rule revision: exactly one approval may win (lost-update regression, found on PostgreSQL)."""
import threading

from sqlalchemy import select

from app.core.errors import ApiError
from app.db.base import SessionLocal
from app.db.models_core import ApprovalRow
from app.engines.regulatory import registry
from tests.engines.regulatory.helpers import P


def test_concurrent_approvals_of_same_revision_exactly_one_wins(env):
    r = env.draft()
    registry.submit(env.db, env.maker, r.rule_id, "ready", None)
    env.db.commit()
    rev = registry.get_rule(env.db, env.tenant, r.rule_id).revision
    results: list = []

    def approve(n):
        with SessionLocal() as s:
            try:
                registry.review(s, P(f"checker-{n}", env.tenant, "checker"), r.rule_id, "APPROVE", "independent review ok", rev)
                s.commit()
                results.append("ok")
            except ApiError as e:
                s.rollback()
                results.append(e.code)
            except Exception as e:  # noqa: BLE001
                s.rollback()
                results.append(type(e).__name__)

    th = [threading.Thread(target=approve, args=(i,)) for i in range(5)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert results.count("ok") == 1, results
    with SessionLocal() as s:
        approvals = s.execute(select(ApprovalRow).where(ApprovalRow.target_id == r.rule_id, ApprovalRow.decision == "APPROVED")).scalars().all()
    assert len(approvals) == 1


# ------------------------------------------------------------------ round 2: every other read-check-write path (PostgreSQL-found class)
from app.core.errors import ApiError as _ApiError  # noqa: E402
from app.db.models_core import ApprovalRow as _AR  # noqa: E402,F401
from app.engines.evidence.models import EvEvent  # noqa: E402
from app.engines.regulatory.models import RegRule, RegSource  # noqa: E402
from tests.engines.regulatory.helpers import dt  # noqa: E402


def _run_threads(n, fn):
    out, errs = [], []

    def w(i):
        try:
            out.append(fn(i))
        except _ApiError as e:
            out.append("API:" + e.code)
        except Exception as e:  # noqa: BLE001 - an unhandled exception type is itself the finding
            errs.append(f"{type(e).__name__}: {e}"[:200])

    th = [threading.Thread(target=w, args=(i,)) for i in range(n)]
    [t.start() for t in th]
    [t.join() for t in th]
    return out, errs


def test_concurrent_activation_of_two_versions_never_leaves_two_active(env):
    """v1 ACTIVE; v2 and v3 approved; activate both at once. Repeated over several keys to widen the race window."""
    for k in range(6):
        key = f"KACT{k}"
        env.active(key=key, eff="2026-01-01T00:00:00Z")
        ids = []
        for eff in ("2026-03-01T00:00:00Z", "2026-06-01T00:00:00Z"):
            r = env.approved(key=key, eff=eff)
            ids.append(r.rule_id)
        env.db.commit()

        def act(i):
            with SessionLocal() as s:
                try:
                    registry.activate(s, P("checker-9", env.tenant, "checker"), ids[i], None, "go", as_of=dt("2026-08-01T00:00:00Z"))
                    s.commit()
                    return "ok"
                except Exception:
                    s.rollback()
                    raise

        out, errs = _run_threads(2, act)
        assert not errs, errs
        with SessionLocal() as s:
            active = s.execute(select(RegRule).where(RegRule.tenant_id == env.tenant, RegRule.rule_key == key, RegRule.status == "ACTIVE")).scalars().all()
        assert len(active) == 1, (key, out, [(a.version, a.status) for a in active])
        assert active[0].version == (3 if "ok" in out and out.count("ok") == 2 else active[0].version)


def test_concurrent_creation_of_same_key_gives_unique_versions_and_clean_conflicts(env):
    env.db.commit()
    # (a) key already exists: creations serialise on the key lock -> all succeed with DISTINCT versions
    env.draft(key="KCREATE")
    env.db.commit()

    def create(i):
        with SessionLocal() as s:
            try:
                r = registry.create_rule(s, env.maker, env.body(key="KCREATE"))
                s.commit()
                return r.version
            except Exception:
                s.rollback()
                raise

    out, errs = _run_threads(5, create)
    assert not errs, errs
    assert sorted(out) == [2, 3, 4, 5, 6], out
    # (b) first-ever creation of a brand-new key by several threads: only clean REVISION_CONFLICTs, unique versions
    out2, errs2 = _run_threads(5, lambda i: _first(env, "KFIRST"))
    assert not errs2, errs2
    oks = [o for o in out2 if isinstance(o, int)]
    assert oks and len(set(oks)) == len(oks) and all(o == "API:REVISION_CONFLICT" for o in out2 if not isinstance(o, int)), out2


def _first(env, key):
    with SessionLocal() as s:
        try:
            r = registry.create_rule(s, env.maker, env.body(key=key))
            s.commit()
            return r.version
        except Exception:
            s.rollback()
            raise


def test_concurrent_upload_of_same_bytes_dedupes_without_error(env):
    data = b"Concurrent upload test document. The member shall hold 42 lots.\n"
    env.db.commit()

    def up(i):
        with SessionLocal() as s:
            try:
                src, dup = registry.ingest_document(s, env.maker, data, None, {"authority_type": "SYNTHETIC", "title": "dup"})
                s.commit()
                return (src.source_id, dup)
            except Exception:
                s.rollback()
                raise

    out, errs = _run_threads(5, up)
    assert not errs, errs
    assert len({o[0] for o in out}) == 1 and sum(1 for o in out if not o[1]) == 1, out
    with SessionLocal() as s:
        n = s.execute(select(EvEvent).where(EvEvent.tenant_id == env.tenant, EvEvent.action == "SOURCE_INGESTED",
                                            EvEvent.entity_id == out[0][0])).scalars().all()
        assert len(n) == 1


def test_concurrent_advance_states_flips_a_scheduled_rule_exactly_once(env):
    r = env.approved(key="KADV", eff="2026-12-01T00:00:00Z")
    registry.activate(env.db, env.checker, r.rule_id, None, "go", as_of=dt("2026-08-01T00:00:00Z"))  # SCHEDULED
    env.db.commit()
    assert env.db.get(RegRule, r.rule_id).status == "SCHEDULED"

    def adv(i):
        with SessionLocal() as s:
            try:
                ch = registry.advance_states(s, env.checker, dt("2026-12-15T00:00:00Z"))
                s.commit()
                return len(ch)
            except Exception:
                s.rollback()
                raise

    out, errs = _run_threads(4, adv)
    assert not errs, errs
    assert sum(out) == 1, out
    with SessionLocal() as s:
        evs = s.execute(select(EvEvent).where(EvEvent.tenant_id == env.tenant, EvEvent.action == "RULE_BECAME_EFFECTIVE",
                                              EvEvent.entity_id == r.rule_id)).scalars().all()
    assert len(evs) == 1
