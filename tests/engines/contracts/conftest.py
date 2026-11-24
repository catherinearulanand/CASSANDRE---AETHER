import json
import uuid
from pathlib import Path

import pytest

from app.auth.security import Principal
from app.db.base import SessionLocal
from app.engines.contracts import fixtures as F

ROOT = Path(__file__).resolve().parents[3]
GOLD = json.loads((ROOT / "fixtures" / "expected" / "contracts" / "gold_fields.json").read_text(encoding="utf-8"))


def principal(tenant, uid, *roles):
    return Principal(uid, tenant, uid, uid, tuple(roles), (), "")


@pytest.fixture()
def tenant():
    return f"ct-{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def db():
    with SessionLocal() as s:
        yield s
        s.rollback()


@pytest.fixture()
def maker(tenant):
    return principal(tenant, "u-maker", "maker")


@pytest.fixture()
def maker2(tenant):
    return principal(tenant, "u-maker2", "maker")


@pytest.fixture()
def checker(tenant):
    return principal(tenant, "u-checker", "checker")


@pytest.fixture()
def admin(tenant):
    return principal(tenant, "u-admin", "tenant_admin")


@pytest.fixture()
def seeded(db, tenant):
    out = F.seed_fixtures(db, tenant)
    db.commit()
    return out


def spec_by_ns(ns):
    return next(s for s in F.SPECS if s["netting_set_id"] == ns)


def new_pkg(db, maker, ns="NS-DEMO-G25", mode="RULES", corr="c1"):
    from app.engines.contracts import services as S
    pkg = S.create_package(db, maker, corr, F.package_body(spec_by_ns(ns)))
    db.commit()
    return pkg
