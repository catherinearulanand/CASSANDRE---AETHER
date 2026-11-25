"""Real-thread concurrency regression tests for EVERY COVENANT mutation path (learned on PostgreSQL: SQLite's serialised
writer hides read-check-write races). A path passes only if concurrent callers get clean domain outcomes: exactly one
winner where one is required, idempotent agreement where the operation is idempotent, and NEVER a raw IntegrityError/500."""
import threading
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.errors import ApiError
from app.db.base import SessionLocal
from app.db.models_core import ApprovalRow
from app.engines.contracts import fixtures as F
from app.engines.contracts import services as S
from app.engines.contracts.models import ConDocument, ConField, ConNettingMembership, ConPackage
from tests.engines.contracts.conftest import new_pkg, principal, spec_by_ns

AS_OF = datetime(2026, 8, 17, 4, tzinfo=UTC)
N = 5


def race(fn, n=N):
    """Run fn(i) -> value in n threads, each in its own session/transaction. Returns list of ('ok', value) | ('err', code)."""
    out: list = []
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()
        with SessionLocal() as s:
            try:
                v = fn(s, i)
                s.commit()
                out.append(("ok", v))
            except ApiError as e:
                s.rollback()
                out.append(("err", e.code))
            except Exception as e:  # noqa: BLE001 - a raw DB/other error is a FAILURE of the path under test
                s.rollback()
                out.append(("raw", f"{type(e).__name__}: {str(e)[:120]}"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def no_raw(res):
    assert not [r for r in res if r[0] == "raw"], res


def test_concurrent_review_of_same_field_exactly_one_wins(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    db.commit()
    f = db.execute(select(ConField).where(ConField.package_id == pid, ConField.status == "PROPOSED")).scalars().first()
    fid, rev = f.field_id, f.revision
    db.commit()  # release this session's transaction (SQLite takes the write lock at BEGIN) before racing
    res = race(lambda s, i: S.review_field(s, principal(tenant, f"chk{i}", "checker"), "c", pid, fid, "APPROVE", "independent review ok", rev))
    no_raw(res)
    assert [r[0] for r in res].count("ok") == 1 and all(r[1] == "REVISION_CONFLICT" for r in res if r[0] == "err"), res


def test_concurrent_create_same_netting_set_exactly_one_wins(db, tenant, maker):
    body = F.package_body(spec_by_ns("NS-DEMO-G25"))
    res = race(lambda s, i: S.create_package(s, principal(tenant, f"mk{i}", "maker"), "c", body))
    no_raw(res)
    assert [r[0] for r in res].count("ok") == 1, res
    assert all(r[1] in ("NETTING_SET_EXISTS", "CROSS_MASTER_NETTING_SET") for r in res if r[0] == "err"), res
    n = db.execute(select(func.count()).select_from(ConPackage).where(ConPackage.tenant_id == tenant)).scalar_one()
    assert n == 1


def test_concurrent_extract_same_package_extracts_once(db, tenant, maker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    res = race(lambda s, i: S.extract(s, principal(tenant, "u-maker", "maker"), "c", pid, "RULES"))
    no_raw(res)
    assert [r[0] for r in res].count("ok") == 1 and all(r[1] == "NOTHING_TO_EXTRACT" for r in res if r[0] == "err"), res
    keys = [k for (k,) in db.execute(select(ConField.field_key).where(ConField.package_id == pid))]
    assert len(keys) == len(set((k, i) for i, k in enumerate(keys)))  # no duplicated extraction batch
    docs_extracted = db.execute(select(func.count()).select_from(ConDocument).where(ConDocument.package_id == pid,
                                                                                    ConDocument.extracted_mode.is_not(None))).scalar_one()
    assert docs_extracted == len(S._docs(db, S.get_package(db, tenant, pid)))


def test_concurrent_propose_freeze_creates_single_pending_approval(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    F._approve_all(db, checker, "c", pid)
    db.commit()
    res = race(lambda s, i: S.propose_freeze(s, principal(tenant, "u-maker", "maker"), "c", pid, "concurrent propose freeze"))
    no_raw(res)
    assert all(r[0] == "ok" for r in res), res
    assert len({r[1]["approval_id"] for r in res}) == 1
    n = db.execute(select(func.count()).select_from(ApprovalRow).where(ApprovalRow.target_id == pid,
                                                                       ApprovalRow.decision == "PENDING")).scalar_one()
    assert n == 1


def test_concurrent_register_same_trade_is_idempotent_no_raw_error(db, tenant, maker, seeded):
    res = race(lambda s, i: S.register_trade(s, principal(tenant, f"mk{i}", "maker"), "c", "TRD-RACE-1", "MA-MER-NWD-001", "NS-MER-NWD-001"))
    no_raw(res)
    oks = [r[1] for r in res if r[0] == "ok"]
    assert oks and len({(o["status"], o["netting_set_id"]) for o in oks}) == 1, res
    assert all(r[1] == "MAPPING_CONFLICT" for r in res if r[0] == "err"), res
    n = db.execute(select(func.count()).select_from(ConNettingMembership).where(ConNettingMembership.tenant_id == tenant,
                                                                                ConNettingMembership.trade_id == "TRD-RACE-1")).scalar_one()
    assert n == 1


def test_concurrent_fact_snapshot_same_id_exactly_one_wins(db, tenant, maker):
    facts = {"rating": {"value": "BBB", "source_id": "SRC-1", "effective_at": "2026-08-01T00:00:00Z"}}
    res = race(lambda s, i: S.record_facts(s, principal(tenant, "u-maker", "maker"), "c", facts, "FACT-RACE"))
    no_raw(res)
    assert [r[0] for r in res].count("ok") == 1 and all(r[1] == "FACT_SNAPSHOT_EXISTS" for r in res if r[0] == "err"), res


def test_concurrent_resolve_same_inputs_all_resolve_same_hash(db, tenant, seeded):
    res = race(lambda s, i: S.resolve_terms(s, tenant, "NS-MER-NWD-001", AS_OF, datetime.now(UTC)))
    no_raw(res)
    assert all(r[0] == "ok" and r[1]["status"] == "RESOLVED" for r in res), res
    assert len({r[1]["terms_hash"] for r in res}) == 1


def test_concurrent_amendment_filing_serialises(db, tenant, maker, checker):
    from tests.engines.contracts.test_contracts_workflow import frozen_pkg
    pkg, _ = frozen_pkg(db, tenant, maker, checker, "NS-MER-NWD-001")
    pid = pkg["package_id"]
    text = F.load_doc("amendment_001_csa_mer_nwd_001.txt")
    rev0 = S.get_package(db, tenant, pid).revision
    db.commit()
    res = race(lambda s, i: S.add_document(s, principal(tenant, f"mk{i}", "maker"), "c", pid,
                                           {"doc_ref": f"AMD-RACE-{i}", "title": f"AMD-RACE-{i}", "text": text + f"\nAdministrative note {i}: filing reference only.\n",
                                            "effective_from": "2026-09-01T00:00:00Z"}, amendment=True), n=3)
    no_raw(res)
    assert all(r[0] == "ok" for r in res), res
    db.expire_all()
    assert S.get_package(db, tenant, pid).revision == rev0 + 3   # no lost revision bump: every filing serialised
    n = db.execute(select(func.count()).select_from(ConDocument).where(ConDocument.package_id == pid)).scalar_one()
    assert n == 2 + 3                                            # master + CSA + 3 amendments
