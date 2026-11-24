# AETHER 2.0 (Cassandre) - synthetic-data derivatives decision-support overlay

**Status: work in progress. All data is synthetic. Nothing here is a regulatory approval, certified model,
live connection, or production system. No trade, settlement or regulatory submission is ever transmitted.**

Specification: `AETHER_MASTER_BUILD_GUIDELINES.md` (provided by the product owner; not yet committed to this repo).
See `docs/adr/` and `docs/status.md` for the honest status matrix.

## Quick start (backend, local)
```
cd backend
pip install -e .[dev]
python scripts/seed.py            # creates .demo-credentials (random passwords, gitignored)
python -m uvicorn app.main:app --port 8000
python -m pytest ../tests -q
```
