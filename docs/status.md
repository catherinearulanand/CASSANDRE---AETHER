# Status matrix (honest; updated by the lead)

Legend: DONE = implemented and verified by a command whose output is stored; IN PROGRESS; NOT STARTED. No gate is green without evidence.

| Area | State | Evidence |
|---|---|---|
| Repo, ADRs, frozen contracts v1 | DONE | `docs/adr/`, `docs/agent-handoffs/CONTRACTS.md` |
| Money/Decimal types, canonical hashing | DONE | `tests/unit/test_core.py` (7 pass, incl. G03 rounding) |
| Auth (Argon2id, sessions, CSRF, RBAC, login rate limit), tenancy, idempotency (G16, G19) | DONE (baseline) | `tests/security/test_auth_tenancy.py` |
| Evidence append foundation, tamper + anchored-truncation detection, concurrent append | DONE (baseline) | `tests/integration/test_evidence_chain.py` |
| Outbox/inbox/DLQ plumbing | CODE WRITTEN, NOT YET TESTED | `app/core/events.py` |
| Run state machine | CODE WRITTEN, state-machine unit tests pending | `app/orchestration/runs.py` |
| Frontend shell, tokens, Overview, Scenario Lab, About, Methodology | DONE (typecheck + production build pass); NOT yet visually verified in a browser | `next build` output |
| Six engines (REGULUS, COVENANT, VECTOR, ATLAS, SENTINEL, VERITAS) | IN PROGRESS (six workers running in a shared tree) | pending reports |
| Alembic migrations | NOT STARTED (tables currently created via `create_all`) | - |
| PostgreSQL run / Docker Compose | NOT VERIFIED (no PostgreSQL; Docker daemon was not running) | - |
| Playwright e2e, screenshots, accessibility audit | NOT STARTED | - |
| Benchmarks (spec section 14) | NOT STARTED | - |
| Backup/restore, offline verifier | assigned to A6 | pending |

## Disclosed deviations
See `docs/adr/0001-stack-and-deviations.md` (SQLite local default; Python 3.14; no QuantLib/pgvector).
The master specification file itself is not yet committed to the repo (it was supplied in the chat).
