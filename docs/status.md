# Status matrix (honest; updated by the lead)

Legend: VERIFIED = a command run on this machine passed (SQLite, Windows, Python 3.14). BUILT = code exists, tests pass, but a stated part is unverified. No gate is green without evidence.

Latest full run: `cd backend; python -m pytest ../tests -q` -> **469 passed, 0 failed, 0 skipped** (SQLite). `npx tsc --noEmit` clean; `npx next build` passes all 12 routes.

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
| UI (six engines + shell) | BUILT, NOT VISUALLY VERIFIED | typecheck + production build only. No browser, Playwright, screenshots, contrast or mobile checks |
| Alembic migrations | NOT STARTED | tables created via `create_all` |
| PostgreSQL / Docker Compose | NOT VERIFIED | no PostgreSQL here, Docker daemon was down; row-lock paths never executed |
| Playwright e2e, accessibility audit | NOT STARTED | |
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
