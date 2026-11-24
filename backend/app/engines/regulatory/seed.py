"""CLI: python -m app.engines.regulatory.seed [--tenant tenant-a]  (run from backend/). Idempotent, synthetic only."""
import argparse

from app.db.base import SessionLocal, create_all
from app.engines.regulatory.fixtures import seed_fixtures

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="tenant-a")
    a = ap.parse_args()
    create_all()
    with SessionLocal() as s:
        print(seed_fixtures(s, a.tenant))
        s.commit()
