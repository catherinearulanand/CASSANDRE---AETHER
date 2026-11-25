# A3 VECTOR — Risk and Margin methodology (method version `vector/1.0.0`)

**Scope:** synthetic data only. Illustrative decision-support calculations. Not SIMM, not SPAN, not certified, not a regulatory margin.
A3 is the sole producer of financial margin results; no LLM computes any figure.

## Numerics
Authoritative arithmetic: Python `decimal`, 40-digit precision. Money is a decimal string + ISO currency; floats are rejected at every
boundary (`app.core.money`, canonical hashing). The only float use is Black-Scholes scenario revaluation of ETD options (`math.erf`),
quantised to 2 dp; closed-form error is double-precision (~1e-12 relative).

## OTC (market `OTC`, methodology `OTC_VM_SIMPLE_V1`)
* Curve time `t = days/365` (ACT/365F). Discount factors log-linear in `t`, `DF(0)=1`; **no extrapolation** beyond the last node (error).
* **IRS** (vanilla fixed/float): PV = signed discounted cashflows. Future float coupon `N*(DFp(s)/DFp(e)-1)` discounted on the discount curve.
  Coupons already reset require a snapshot fixing `trade_id|period_start`; **a missing fixing is an error, never ignored**.
  Schedules are month-anchored with **no business-day adjustment / holiday calendar** and no stub periods (documented limitation).
* **FX forward** (buy `N` base at `K` quote/base): `PV_quote = N*(S*DF_base - K*DF_quote)`; SELL negates; converted to the CSA base currency
  with the pinned FX (orientation explicit; reciprocal of the reverse pair is used only if the direct pair is absent; a missing pair is an error).
* **Netting** only within a netting set; all trades in a set must share one master (else BLOCKED). Reported: gross positive, gross negative, net,
  and `netting_benefit = gross_positive - max(net, 0)` (reduction in positive credit exposure; not notional, not realised cash saving).
* **Waterfall** (perspective: Party A = us). `E>0` ⇒ Party B is obligor. `target = max(0, |E| + IA - Threshold)` using the obligor's directional terms;
  `delta = target - held_same_direction`; MTA tested on the **unrounded** `|delta|` (`GTE` or `GT` per terms); then agreement-specific rounding
  (calls: `rounding_mode`; returns: `return_rounding_mode`, else explicit-assumption `DOWN`). Collateral held from the opposite party is returned as a
  **separate** obligation and never offsets the new direction. Segregated IM is a separate illustrative notional-bucket schedule (`HYPOTHETICAL`, not SIMM) and is never netted against VM.
* Terms come only from `contracts.resolve_terms` (approved, hashed). A mismatching `terms_hash`, missing/invalid material term, or unavailable contracts
  service ⇒ `BLOCKED` with reasons. No defaults. No `rule_set_hash` ⇒ result is flagged contract-driven; regulatory IM/VM applicability is UNKNOWN.

## ETD (markets `ETD_DOMESTIC` INR / `ETD_IFSC` USD, methodology `ETD_IMPORTED_V1`)
* The imported clearing-style margin is the **source obligation**: independent sum of `ADDITIVE` minus `OFFSET` components, reconciled to the file's reported
  total (`INFO` rows never summed; `REPORTED_TOTAL` compared, never summed). Mismatch ⇒ `EXCEPTION`, calculation BLOCKED. Files are validated atomically
  (header, strict decimals, currency, duplicate component, mixed accounts/currencies, formula-injection prefixes, size, optional sha256 checksum).
  Same `(file_id, revision)` + same content is idempotent; different content ⇒ 409; revised files need a new revision.
* Domestic (`DEMO_NSE`, INR) and IFSC (`DEMO_IFSC`, USD) profiles are independent; cross-profile venue/currency ⇒ BLOCKED/validation error.
* A rule revision enters only as a pinned multiplier from `regulatory.rule_parameters` or an explicit `hypothetical_margin_multiplier` (flagged `HYPOTHETICAL`):
  `required = imported_total * multiplier` (components `IMPORTED` + `RULE_ADJUSTMENT` reconcile to the required amount).
* The **internal estimate** (futures `lots*multiplier*mark*shock`; European options by Black-Scholes revaluation; worst of ±shock) and position metrics
  (gross/net lots, notional, delta-equivalent lots) are labelled `INTERNAL_ESTIMATE_NOT_SOURCE_OBLIGATION_NOT_SPAN` and are never mixed with the obligation.
* `CCP_OTC` ⇒ `UNSUPPORTED` (bilateral CSA waterfall is not reused for centrally cleared OTC).

## Data-quality gates
Snapshots are immutable, content-hashed and validated (strict time grid, positive DF, FX reciprocal tolerance; negative rates are legal). A snapshot older than its
`max_age_seconds` relative to the run `as_of`, dated after `as_of`, or missing ⇒ BLOCKED (`STALE_MARKET`); a later valid snapshot requires a new run. Expired trades in a
set ⇒ BLOCKED. Unknown is never aggregated as zero.

## Stress
Deterministic shocks (parallel curve shift in bp, FX shock fractions, price shock, additive vol shock) are applied to a **copy** of the pinned snapshot; the
baseline result and snapshot are never mutated. OTC reports baseline vs scenario exposure/required transfer, per-trade deltas and DV01 (finite difference: +1 bp
parallel bump, full revaluation). ETD reports internal P&L only.

## Caching, persistence and evidence
Result cache key = tenant + snapshot hash + terms hash + rule-set hash + trade/file hash + context + method version. `BLOCKED` results are stored but never served as
cache hits. Evidence is appended in the same DB transaction as every write (`SNAPSHOT_PINNED`, `PORTFOLIO_IMPORTED`, `CLEARING_FILE_IMPORTED`, `CLEARING_RECONCILED`,
`MARGIN_COMPUTED|MARGIN_BLOCKED|MARGIN_UNSUPPORTED`, `STRESS_RUN`); if evidence is unavailable the transaction rolls back (no result persists).

## Independent verification
`fixtures/expected/risk/hand_cases.json` (paper hand calculations) and `irs_2y_semi.json` (separate float reference `_reference_calc.py`) are never imported by production code.

## Known limitations
No holiday calendars; no stubs; single-currency-per-leg vanilla IRS only; no exotics; no QuantLib cross-check (not installed); IM is demonstrative only; PostgreSQL and
SQLite row-lock behaviour differ and only SQLite was exercised here; the numeric benchmark covers SQLite only.

## Round 2 additions (contract v1.1)

- `MarginResult.as_of` (business time) and `snapshot_as_of` (the pinned snapshot's own data time; null when unknown). Bitemporal: the OTC desk passes `known_at` (recording time of terms resolution, returned by `/risk/terms/resolve`).
- `risk.get_positions(account, as_of)`: gross/net lots, notional, delta-equivalent lots; unknown values are `null` with a warning, never zero; `client_class` is always `UNKNOWN` (not an input of this engine). Expired options give null notional/delta with an explicit expiry reason.
- `risk.reconciliation_status(account, as_of)`: recomputed from stored rows; `OK`, `EXCEPTION`, or `UNKNOWN` (no file, or no reported total).
- Evidence: each OK result event carries `payload.parents` (snapshot, clearing file, terms hash, rule set) and `payload.replay`; replayer `replay.risk` recomputes with the cache bypassed and the original trades / clearing-file revision pinned. The business hash excludes run identity and the float internal estimate, which is compared with tolerance 0.01.
- Result cache: partial unique index `(tenant_id, cache_key) WHERE status IN ('OK','UNSUPPORTED')`; concurrent identical computes return the winner's immutable result and write one event.
- Numerics: normal CDF uses `erfc` (the previous `0.5*(1+erf)` cancelled to exactly 0.0 for d1 < about -8). Float BSM is reconciled in tests to an independent adaptive-precision Decimal oracle (validated against mpmath for pi).
