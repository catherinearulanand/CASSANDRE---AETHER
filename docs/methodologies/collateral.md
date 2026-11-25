# ATLAS — Collateral and liquidity optimisation: methodology (A4)

Synthetic data only. ATLAS **recommends**; it never moves assets, posts collateral or transmits anything. "Reserved" is an
internal hold on synthetic inventory; "posted" exists only through an explicitly labelled **SIMULATED** settlement record.

## 1. Inputs and obligation
* Margin result: `risk.get_result` (A3 is the only producer). Must be `status == OK`; `UNSUPPORTED`/`CCP_OTC`/blocked ⇒ explicit `Blocked`, no allocation.
* Required coverage to post — **OTC**: `rounded_amount` when `direction == POST`, else 0 (RETURN / NONE ⇒ no allocation).
  **ETD**: `required_amount − eligible collateral already posted` (floored at 0). Only *posted* collateral counts, never proposed/reserved.
* Market and currency must match the inventory snapshot (`MARKET_MISMATCH`, `CURRENCY_MISMATCH`): domestic INR and IFSC USD are never blended.
* If the margin result is `hypothetical`, the proposal is labelled HYPOTHETICAL and can never be approved or reserved.

## 2. Eligibility (intersection, default deny)
A lot is usable only if all hold: explicit entitlement (`entitled_to` contains the obligation id, no pooling), issuer group not on the explicit
wrong-way list (relationship data, not name similarity), a matching entry in **every** active policy source (CONTRACT schedule from
`contracts.resolve_terms` for OTC, CLEARING_POLICY attached to the snapshot, REGULATORY overlay from `regulatory.rule_parameters` when a
`rule_set_hash` is given), known price > 0, FX when cross-currency, rating/maturity known when the entry restricts them, settlement by the
deadline (`as_of + settlement_lag_days ≤ deadline`, calendar days in P0), and free units. Unknown ⇒ excluded **with a reason code**; nothing is defaulted.
Schedule match = first entry in file order matching asset_class / currency / issuer_class.

## 3. Valuation and cost
`e_i = price_i · FX_i · valuation_factor_i · fx_factor_i` (target currency per unit).
* Percent fields are **percentage points** (94 = 94 %). One discount convention per entry: `valuation_pct` XOR `100 − haircut_pct`; if both are given they must sum to 100 (else `INCONSISTENT_HAIRCUT`) — a 6 % haircut is never applied on top of a 94 % valuation.
* `fx_factor = (100 − fx_haircut_pct)/100` only when lot currency ≠ obligation currency; missing ⇒ `MISSING_FX_HAIRCUT` (excluded).
* Several sources ⇒ the **minimum** factor (most conservative); discounts are never multiplied.
* Cost per unit (target ccy, horizon, **not annualised**): `price · FX · annual_cost_rate · horizon_days/365 + txn_cost_per_unit`. `annual_cost_rate` is a decimal fraction p.a.; missing ⇒ `UNKNOWN_COST`. Cash is not assumed to be the most expensive asset.

## 4. Optimisation (HiGHS via SciPy)
Variables are scaled so the obligation is 1. Divisible lots: continuous quantity. Indivisible lots (`lot_size > 0`): integer block counts ⇒ MILP.
Minimise Σ cost·x subject to: coverage ≥ required; bounded over-posting (also keeps negative-cost lots bounded); `0 ≤ x ≤ available`;
caps — fraction of **total allocated** coverage is linearised `Σ_g e x − cap·Σ_all e x ≤ 0`, fixed **obligation** denominator `Σ_g e x ≤ cap·R`;
issuer-group caps likewise. Deterministic tie-break: after the primary optimum a secondary LP minimises a lot_id-rank-weighted coverage share
under the primary objective; solver-tolerance dust (< 1e-7 of the obligation) is folded into the largest lot.
Statuses: `optimal | feasible | infeasible | timeout | error`. `feasible` = MILP incumbent at time limit **or** a repaired rounding (optimality not claimed).
Shadow price: dual of the coverage row, continuous LP only, unit = target-currency horizon cost per unit of effective coverage, other rows held fixed; **never** reported for MILP.
Infeasible ⇒ auxiliary LP reports max achievable coverage, remaining shortfall and binding constraints; legal limits are not relaxed.

## 5. Rounding and independent verification
Quantities are rounded to 6 dp (UP after snapping float noise to 9 dp; whole blocks for indivisible lots) and clipped to availability. If the verifier
rejects that, a second attempt uses nearest rounding plus a conservative top-up (a *repair*, ⇒ status `feasible`). `verifier.py` recomputes in Decimal,
sharing **no code** with the solver path: uniqueness, precision, inventory, entitlement, wrong-way, settlement, eligibility under every source, effective coverage,
coverage ≥ required, over-posting ≤ allowance, every cap. Any failure blocks approval.

## 6. Baselines and savings
B1 = cash only; B2 = cash first then remaining eligible lots in lot_id order. A baseline is used only if it passes the same verifier. If none is feasible, **no savings are claimed**.
Savings are modelled horizon cost, not realised savings; zero/negative means no improvement (cash-optimal and no-improvement cases are tested). B1/B2 are heuristics, not an institution's actual policy.

## 7. Lifecycle, approval, reservation
`PROPOSED → CHECKED → APPROVED → RESERVED → DRAFTED`, with `CANCELLED`, `EXPIRED`, and terminal `INFEASIBLE`, `NOT_REQUIRED`, `FAILED`; stress runs are `HYPOTHETICAL`.
Approval: independent checker (`require_distinct_checker`), mandatory reason, binds proposal hash + revision, expires (`approval_ttl_seconds`), `expected_revision` optimistic lock.
Reserve re-checks: approval hash/revision/expiry, margin result hash (fail closed if risk is unavailable), OTC terms hash, inventory snapshot hash, and re-runs the verifier against *current* availability; then each lot is
reserved with a conditional `UPDATE … WHERE units − encumbered − reserved ≥ q` plus a unique `(tenant, proposal, lot)` reservation row. Quantities are scaled integers (micro-units) so this is exact.
Cancel/expiry release reservations. Evidence is appended in the same transaction; if the evidence service is unavailable the transition rolls back.

## 8. Limitations (honest)
* Concurrency test (G13) ran on **SQLite** (`BEGIN IMMEDIATE` serialises writers). PostgreSQL row-lock path (`FOR UPDATE`) is coded but **not executed** here.
* Reservations are per snapshot row; a revised inventory file is a new snapshot and does not inherit reservations (P1: lot identity across snapshots).
* P0 solves per authorised pool (one obligation); no global multi-obligation optimisation or entitlement graph.
* Settlement lag uses calendar days, not a venue calendar. Cost model is a documented synthetic assumption.
* Percent unit convention (percentage points) and asset_class vocabulary (`CASH`, `GSEC`, `CORP`) must be shared by contract schedules (see proposals).
* `rule_set_hash` is re-checked indirectly through the margin result hash; regulatory overlay supports `eligible_asset_classes` and `issuer_group_cap_pct` only.
* Market-scope entitlements on the user are not enforced by this engine (role + tenant only).
* The ISO 20022-like sample inside a draft is illustrative, unvalidated and never sent.
* No browser/Playwright run was possible on this machine: UI verified by `tsc`, `next build` and API calls only.

## 9. Gate evidence (tests in `tests/engines/collateral/`, expected values in `fixtures/expected/collateral/golden.json`)
A-A01 `test_g05…`, `test_lp_vs_exhaustive_grid…` · A-A02 verifier + property test · A-A03 G01/G12 exclusions · A-A04 cap + G06 tests ·
A-A05 `test_g07…` · A-A06 zero-call/RETURN · A-A07 stale/expired approval · A-A08 `test_g13…` (threads) · A-A09 cost/baseline tests · A-A10 UI implemented, not browser-verified.

## Round 2 additions

* **Evidence/lineage**: events use entity types `allocation_proposal`, `inventory_snapshot`, `approval`; parents `risk_result`, `inventory_snapshot`, `rule_set`, `terms` in VERITAS format `{entity_type, entity_id}`.
* **Replay (`replay.collateral`)**: recomputes from persisted inputs incl. the availability the original run saw (`params.availability`); compares the business hash (= `proposal_hash`: input hashes, required, horizon, deadline, stress, rounded legs) and objective/residual within declared tolerance 0.000001. `method_version` pins `collateral-lp/1 highspy/<version>`; a different solver version => NOT_REPLAYABLE. Read-only (VERITAS savepoint rollback; test asserts no reservation/proposal/evidence rows change).
* **`collateral.constraint_slacks`**: `{name, kind, limit, used, slack, binding, unit}` per stored constraint; empty list for infeasible proposals.
* **Concurrency**: every mutator takes a proposal row lock before any state/revision check; reservation is one conditional UPDATE inside a savepoint (all-or-nothing, lot_id lock order); first inventory import of the same snapshot id is race-safe.
* **Vocabulary mapping** (until frozen in CONTRACTS): contract `GOVERNMENT_BOND→GSEC`, `CORPORATE_BOND→CORP`, cap denominator `TOTAL_EFFECTIVE_ALLOCATED→TOTAL_ALLOCATED`, issuer class `ANY` = wildcard. Each mapping is shown as a `VOCABULARY_NORMALISED` warning; unknown values are rejected, never coerced.
* **Indivisible lots (P1)**: MILP whole blocks; coverage window [R, R+largest block]; optimal checked against exhaustive enumeration; repair never labelled optimal.
