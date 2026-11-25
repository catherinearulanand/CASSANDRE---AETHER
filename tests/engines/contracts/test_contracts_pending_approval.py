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
