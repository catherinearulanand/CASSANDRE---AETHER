"""PostgreSQL enforces VARCHAR lengths that SQLite ignores. Over-long values from untrusted documents must become validation issues
(UNRESOLVED fields), never a raw database error at flush time."""
import uuid

import pytest

from sqlalchemy import select

from app.engines.contracts import fixtures as F
from app.engines.contracts import services as S
from app.engines.contracts.models import ConEligibility, ConField
from tests.engines.contracts.conftest import spec_by_ns

HDR = ("| Asset | Currency | Issuer | Valuation Percentage | Haircut | FX Haircut | Concentration Cap | Min Rating | Max Maturity |\n")


def _pkg_with_table(db, maker, row):
    text = F.load_doc("csa_demo_g25.txt").replace("| CASH | USD | ANY | 100% | 0% | 0% | none | none | none |", row)
    b = F.package_body(spec_by_ns("NS-DEMO-G25"))
    b["documents"] = [dict(b["documents"][0], text=text)]
    return S.create_package(db, maker, uuid.uuid4().hex, b)["package_id"]


@pytest.mark.parametrize("row", [
    "| " + "X" * 80 + " | USD | ANY | 100% | 0% | 0% | none | none | none |",
    "| CASH | USD | " + "Y" * 80 + " | 100% | 0% | 0% | none | none | none |",
    "| CASH | USD | ANY | 100% | 0% | 0.0000000000000000000000001% | none | none | none |",
    "| CASH | USD | ANY | 100% | 0% | 0% | none | none | 12345678901234567890123456789012345 |",
    "| CASH | USD | ANY | 100% | 0% | 0% | none | " + "Z" * 20 + " | none |",
])
def test_overlong_table_cells_never_reach_the_database_unvalidated(db, tenant, maker, row):
    pid = _pkg_with_table(db, maker, row)
    S.extract(db, maker, uuid.uuid4().hex, pid, "RULES")
    db.commit()
    sched = db.execute(select(ConField).where(ConField.package_id == pid, ConField.field_key == "eligible_schedule")).scalars().one()
    assert sched.status == "UNRESOLVED" and sched.issues, (row[:50], sched.status)
    assert not db.execute(select(ConEligibility).where(ConEligibility.package_id == pid)).first()
