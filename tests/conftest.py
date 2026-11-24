import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp(prefix="aether-test-")
os.environ["AETHER_DATABASE_URL"] = os.environ.get("AETHER_TEST_DATABASE_URL") or f"sqlite:///{Path(_tmp, 't.db').as_posix()}"
os.environ["AETHER_ENVIRONMENT"] = "test"
os.environ["AETHER_AUTO_CREATE_SCHEMA"] = "true"  # tests build the schema from models; migrations are tested separately

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def app_client():
    from app.auth.security import hash_password
    from app.db.base import SessionLocal
    from app.db.models_core import Tenant, User
    from app.main import app

    with SessionLocal() as s:
        for t in ("tenant-a", "tenant-b"):
            s.add(Tenant(tenant_id=t, name=t))
            for u, roles in (("maker", ["maker"]), ("checker", ["checker"]), ("viewer", ["viewer"]), ("admin", ["tenant_admin"])):
                s.add(User(tenant_id=t, username=u, display_name=u, password_hash=hash_password("pw-" + u),
                           roles=roles, entitlements=[]))
        s.commit()
    return TestClient(app)


def login(app_client, tenant, user):
    c = TestClient(app_client.app)
    r = c.post("/api/v1/session/login", json={"tenant_id": tenant, "username": user, "password": "pw-" + user})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = r.json()["csrf_token"]
    return c


def pytest_configure(config):
    config.addinivalue_line("markers", "gold: independent golden-case assertions")
    config.addinivalue_line("markers", "security: tenant/auth security tests")


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from app.db.base import Base, create_all, get_engine, import_all_models, is_sqlite
    if not is_sqlite():  # PostgreSQL run: start from a clean schema so reruns are deterministic
        import_all_models()
        Base.metadata.drop_all(get_engine())
    create_all()
