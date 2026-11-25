"""The independent checker must be able to find the maker's pending freeze approval from a different session
(found in the browser flow: it previously lived only in the maker's UI state)."""
from sqlalchemy import select

from app.engines.contracts import fixtures as F
from app.engines.contracts import services as S
from app.engines.contracts.models import ConField
from tests.engines.contracts.conftest import new_pkg


def test_readiness_exposes_only_the_currently_valid_pending_approval(db, tenant, maker, checker):
    pkg = new_pkg(db, maker)
    pid = pkg["package_id"]
    S.extract(db, maker, "c", pid, "RULES")
    F._approve_all(db, checker, "c", pid)
    db.commit()
    assert S.readiness(db, tenant, pid)["pending_approval"] is None            # nothing proposed yet
    ap = S.propose_freeze(db, maker, "c", pid, "propose freeze for independent check")
    db.commit()
    seen = S.readiness(db, tenant, pid)["pending_approval"]                    # what a checker's session reads
    assert seen and seen["approval_id"] == ap["approval_id"] and seen["maker_id"] == maker.user_id and seen["decision"] == "PENDING"
    # content changes after the proposal (a field is rejected): the approval is stale and must not be offered as actionable
    f = db.execute(select(ConField).where(ConField.package_id == pid, ConField.field_key == "rounding_mode")).scalars().first()
    S.review_field(db, checker, "c", pid, f.field_id, "REJECT", "withdrawing this field after proposal", f.revision)
    db.commit()
    assert S.readiness(db, tenant, pid)["pending_approval"] is None


def test_registry_listing_summaries_equal_an_independent_per_row_computation(db, tenant, seeded):
    """The listing uses batched grouped queries; verify every summary number against raw rows read one package at a time."""
    from sqlalchemy import func

    from app.engines.contracts.models import ConDocument, ConNettingMembership, ConPackage, ConVersion
    items = S.list_packages(db, tenant)["items"]
    assert len(items) == 5
    for it in items:
        pid = it["package_id"]
        pkg = db.get(ConPackage, pid)
        fields = list(db.execute(select(ConField).where(ConField.package_id == pid)).scalars())
        docs = list(db.execute(select(ConDocument).where(ConDocument.package_id == pid)).scalars())
        assert it["fields"] == {s: sum(1 for f in fields if f.status == s) for s in ("PROPOSED", "UNRESOLVED", "APPROVED", "REJECTED")}
        assert it["documents"] == len(docs) and it["docs_pending_extraction"] == sum(1 for d in docs if d.extracted_mode is None)
        assert it["docs_with_missing_annexes"] == sum(1 for d in docs if d.missing_annexes)
        assert it["latest_version_no"] == db.execute(select(func.max(ConVersion.version_no)).where(ConVersion.package_id == pid)).scalar_one()
        assert it["trades_in_netting_set"] == db.execute(select(func.count()).select_from(ConNettingMembership).where(
            ConNettingMembership.tenant_id == tenant, ConNettingMembership.netting_set_id == pkg.netting_set_id,
            ConNettingMembership.status == "MEMBER")).scalar_one()
        assert S.package_summary(db, pkg) == it
    assert {i["netting_set_id"]: i["latest_version_no"] for i in items}["NS-MER-NWD-001"] == 2
    assert [i["netting_set_id"] for i in S.list_packages(db, tenant, needs_review=False)["items"]] == [
        i["netting_set_id"] for i in items if i["outstanding_review"] == 0]
