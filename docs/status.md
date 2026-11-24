# Status matrix (honest; updated by the lead)

Legend: VERIFIED = a command run on this machine passed (SQLite, Windows, Python 3.14). BUILT = code exists, tests pass, but a stated part is unverified. No gate is green without evidence.

Latest full runs: SQLite `cd backend; python -m pytest ../tests -q` -> **470 passed, 3 skipped** (the 3 are PostgreSQL-only migration tests). PostgreSQL 16 (`AETHER_TEST_DATABASE_URL=postgresql+psycopg://...`) -> **472 passed, 1 skipped** (SQLite-only backup/restore, reason stated in the skip). `npx tsc --noEmit` clean; `npx next build` passes all 12 routes.

| Area | State | Evidence / caveat |
|---|---|---|
| Shared baseline (money, canonical hash, auth, tenancy, idempotency, evidence append) | VERIFIED | tests/unit, tests/security, tests/integration |
| Engine A1 REGULUS (regulatory) | BUILT | 105 tests (tests/engines/regulatory); no real law encoded, all rules fictional; no browser run |
| Engine A2 COVENANT (contracts) | BUILT | 100 tests; extractor handles only the fictional clause grammar; no OCR |
| Engine A3 VECTOR (risk/margin) | BUILT | 75 tests; benchmark reports/benchmarks/risk.json (500 trades, warm p95 ~119 ms, SQLite); no holiday calendars; IM is demonstrative, not SIMM |
| Engine A4 ATLAS (collateral) | BUILT | 81 tests; HiGHS LP; concurrency tested with real threads on SQLite only; benchmark in reports/benchmarks/collateral*.json (one contended run missed the p95 target) |
| Engine A5 SENTINEL (compliance) | BUILT | 60 tests; 3 of 9 controls UNSUPPORTED (listed); no scheduled runs; surveillance metrics are synthetic-benchmark only |
| Engine A6 VERITAS (evidence) | BUILT | 41 tests; signed checkpoints, bundles, offline verifier; local file key, not an HSM; replay says NOTHING_TO_REPLAY until engines emit `payload.replay` |
| Cross-engine chain, real services, no fakes (tests/integration/test_full_chain.py) | VERIFIED | G02 (0 transfer), G03 (19,469,510.12 -> 9,469,510.12 -> 9,500,000 POST), G21 (stale snapshot blocked), G01 (10,000,000 -> 12,500,000 INR hypothetical; posted 11,500,000; shortfall 1,000,000 -> compliance FAIL with trace, exactly one case, no duplicate on repeat; collateral allocates for the 1,000,000 gap) |
| Integration fix found by the chain test | FIXED | SENTINEL's coverage control pinned `DEMO-ETD-COVERAGE` v2, REGULUS seeds only v1 -> compliance returned UNKNOWN. Pin changed to v1 (60 compliance tests still pass) |
| Seed | VERIFIED | `python backend/scripts/seed_demo.py` seeds users + all five engines' fixtures |
| UI (six engines + shell), browser smoke | VERIFIED (smoke) | `python tests/e2e/ui_smoke.py` (Playwright + system Edge): 10 routes x 4 viewports (1440/1280/768/390) = 40 route-views, 0 horizontal overflow, 0 console errors, 0 failed API calls; results in `reports/test-results/ui_smoke.json`, screenshots in `reports/screenshots/`. First run found and I fixed: top-bar grid gap, mobile top-bar overflow, wide audit table overflowing at 768/1280 |
| UI flow: Risk desk G01 hypothetical (browser -> API -> engine) | VERIFIED | `python tests/e2e/ui_flow_g01.py`: UI shows INR 12,500,000 and a HYPOTHETICAL marker |
| UI: remaining flows (approvals, reservations, case workflow, exports, tamper demo), keyboard nav, screen reader, WCAG contrast | NOT VERIFIED | only smoke + one flow so far |
| Alembic migrations | VERIFIED (baseline) | `backend/migrations/versions/0001_baseline_schema.py` (55 tables). `tests/integration/test_migrations.py` on PG16: upgrade from empty, `alembic check` = no drift, downgrade/upgrade round trip, API refuses to start on unmigrated DB and starts on migrated one. Single baseline revision (no per-engine branches); prior-schema upgrade untested because no prior schema exists |
| PostgreSQL 16 full suite | VERIFIED | 472 passed / 1 skipped (above). The PG run found and I fixed 3 real concurrency bugs SQLite had masked: (1) concurrent first evidence append for a new tenant raised a unique violation (now ON CONFLICT DO NOTHING + locked head); (2) compliance case updates: 4 of 5 concurrent writers passed the optimistic revision check (row lock was never applied); (3) regulatory rule review/activate had no row lock: **5 of 5 concurrent approvals of one revision succeeded** before the fix (regression test `tests/engines/regulatory/test_concurrency.py` confirmed to fail without it) |
| Docker Compose | PARTIAL | `compose config` valid; `db` service healthy with no published port. **Image build for migrate/backend NOT verified**: the container could not verify PyPI's TLS certificate on this network (certificate interception); I did not disable verification. No frontend service in compose |
| PostgreSQL: backup/restore, pg_dump, RLS, performance on PG | NOT DONE | backup/restore is SQLite-only; no row-level security; no benchmarks run on PG |
| Full Playwright scenario suite, accessibility audit | PARTIAL | two scripts above only; no axe/contrast audit |
| E-A01 (all six engines emit evidence) | PARTIAL | regulatory, contracts, risk, collateral, compliance verified on the seeded flow; VERITAS audit events not asserted |
| Replay (E-A05) end-to-end | NOT DEMONSTRATED | engines do not yet emit `payload.replay` / `payload.parents` |
| Spec benchmarks (section 14) | PARTIAL | risk, collateral, evidence measured individually; no whole-pipeline or DEMO-tier (500/2,000/1,000) run; SCALE/STRESS not run |
| Outbox/inbox/DLQ, run state machine | CODE ONLY | no dedicated tests yet |
| Run pipeline orchestration in UI (`/runs` advancing through states) | NOT BUILT | runs are created and recorded; engines are not yet driven by the run state machine |

## Known integration notes
- Bitemporal: frozen terms are known only from their freeze time, so OTC calculations need an explicit `known_at` at/after the freeze. UI/orchestration must pass it.
- Hypothetical margin (rule revision) is flagged; ATLAS will not approve/reserve against it.

## Disclosed deviations
See `docs/adr/0001-stack-and-deviations.md` (SQLite local default; Python 3.14; no QuantLib/pgvector). The master specification file is not committed to the repo (it was supplied in the chat).
