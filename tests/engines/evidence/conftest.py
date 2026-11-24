import uuid

import pytest

from app.auth.security import hash_password
from app.core.config import get_settings
from app.db.base import SessionLocal
from app.db.models_core import Tenant, User
from app.engines.evidence import service as ev


@pytest.fixture(scope="session")
def _ev_session_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("ev-store")


@pytest.fixture(autouse=True)
def ev_dirs(_ev_session_dir, monkeypatch):
    """Object store + signing keys + exported checkpoints live in a session-scoped temp dir (the test DB is shared for
    the session, so its object rows must keep resolving to the same store). Tests isolate by using unique tenants."""
    st = get_settings()
    monkeypatch.setattr(st, "object_store_dir", str(_ev_session_dir / "objects"))
    monkeypatch.setattr(st, "signing_key_dir", str(_ev_session_dir / "keys"))
    ev.set_storage_outage(False)
    yield _ev_session_dir
    ev.set_storage_outage(False)


@pytest.fixture
def tenant():
    return "t-" + uuid.uuid4().hex[:10]


def add_events(session, tenant_id, n, correlation="c", **kw):
    out = []
    for i in range(n):
        out.append(ev.append(session, tenant_id, engine="platform", action=kw.get("action", "TEST"), actor_id="u",
                             actor_type="USER", entity_type=kw.get("entity_type", "x"), entity_id=f"{kw.get('prefix', '')}{i}",
                             correlation_id=correlation, payload=kw.get("payload"), source_spans=kw.get("source_spans"),
                             input_hashes=kw.get("input_hashes"), rationale=kw.get("rationale")))
    return out


@pytest.fixture(scope="session")
def audit_users(app_client):
    """Auditor + compliance users in both tenants (the shared conftest seeds only maker/checker/viewer/admin)."""
    with SessionLocal() as s:
        for t in ("tenant-a", "tenant-b"):
            for u, roles in (("auditor", ["auditor"]), ("compliance", ["compliance_officer"])):
                s.add(User(tenant_id=t, username=u, display_name=u, password_hash=hash_password("pw-" + u), roles=roles, entitlements=[]))
        s.commit()
    return True
