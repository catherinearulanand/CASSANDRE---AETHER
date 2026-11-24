import pytest

from tests.engines.regulatory.helpers import Env


@pytest.fixture()
def env():
    from app.db.base import SessionLocal
    with SessionLocal() as db:
        e = Env(db)
        yield e
        db.rollback()
