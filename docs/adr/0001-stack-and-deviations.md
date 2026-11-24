# ADR 0001 - Stack and disclosed deviations

Status: accepted (lead). Modular monolith: FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic; Next.js/TypeScript frontend.

## Deviations from the master spec (disclosed)
- **Database**: the build machine has no PostgreSQL and Docker was not running. Local default is SQLite (WAL, `BEGIN IMMEDIATE`
  so writers serialise). PostgreSQL is supported via `AETHER_DATABASE_URL` and `infra/compose.yaml`; row locks use
  `SELECT ... FOR UPDATE` there. The full test suite HAS now been run against PostgreSQL 16 (Docker) - see `docs/status.md`. The compose image build is unverified (TLS interception blocked pip inside the container). Migrations: Alembic baseline `0001`; PostgreSQL requires `alembic upgrade head` and the API refuses to start on a schema mismatch; SQLite/tests use `create_all`.
- **Python 3.14** is installed locally (spec default 3.12; code targets >=3.12).
- QuantLib and pgvector are not installed; QuantLib adapter is optional and not required for P0.

## Engine isolation
Engines never import each other. Cross-engine calls go through `app.core.services` (registry). Each engine owns
`app/engines/<key>/{models,api,services}.py` and a unique table prefix. The gateway auto-mounts `api.router`.
