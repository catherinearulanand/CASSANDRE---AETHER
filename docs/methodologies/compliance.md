# SENTINEL — Continuous compliance and surveillance (A5) · methodology

**Scope:** deterministic evaluation of approved *synthetic* controls, case management, and statistical surveillance on *synthetic* events.
It never changes an A1 rule or an A3 calculation, never blocks or places trades, and never submits a regulatory report.
All rules in the control library are `SYNTHETIC_POLICY` (fictional), not statute.

## 1. Evaluation
* A control = an approved rule version (key, version, hash, authority) + scope AST + condition AST + required inputs + severity + max input age.
* Evaluator: typed, bounded, eval-free three-valued DSL (`app/engines/compliance/dsl.py`) — `all any not eq in gt gte lt lte exists between sum_by ratio abs effective_on`.
  Bounds: depth 12, 400 nodes, 10,000 rows, 1 s. Floats rejected. Units come from the field registry; comparing different units is an **ERROR** (no INR/USD blending).
  Bare strings are field references only if registered; the first operand of a comparison must be a field/expression (typos cannot become literals).
* Outcome per control × subject: `PASS | FAIL | UNKNOWN | NOT_APPLICABLE | CONFLICT | ERROR`.
  * scope FALSE → NOT_APPLICABLE; scope UNKNOWN → UNKNOWN (e.g. unknown client classification, G11).
  * Fail closed: missing/stale/future inputs, wrong measure basis, unavailable upstream service, blocked margin result, currency mismatch → **UNKNOWN**, never PASS.
  * Rule currency is checked against `regulatory.resolve_rules` when that service exists (CONFLICT / UNKNOWN / stale control revision). When absent the trace records `UNVERIFIED` — it is never silently "current".
* Inputs: required margin from `risk.get_result` (status must be OK); eligible **posted** collateral from `collateral.coverage` (a proposal is not posted collateral).
  Callers may supply only an allow-listed set of facts (positions, classification, terms status, input timestamp…), labelled `CALLER_SUPPLIED`; margin and coverage amounts cannot be typed in.
  An input problem only affects controls that depend on that input.
* Event-triggered: only control families whose inputs arrived are evaluated (`full_sweep`/`control_keys` override). Untriggered controls stay visibly *unevaluated*; unsupported controls (collateral concentration, settlement deadline, reporting schema) are listed as UNSUPPORTED with a reason.
* Evaluations are idempotent on `eval_key` (control, revision, subject, snapshot, as_of, facts hash, inputs). Each persisted evaluation appends an evidence event in the same transaction.

## 2. Cases
* One case per `(control, subject, rule revision)`; recurrences increment `occurrence_count` and keep every evaluation. A resolved/dismissed case **re-opens** on recurrence. UNKNOWN opens a `DATA_GAP` review case (severity capped at MEDIUM).
* Lifecycle `OPEN → ACKNOWLEDGED → INVESTIGATING → REMEDIATION_PROPOSED → REVIEW_REQUIRED → RESOLVED`; `DISMISSED` is two-person (maker requests with reason, independent checker approves; the approval is bound to the current case hash/revision and is refused as stale if the case changed).
* Remediation records proposed / selected / rejected routes with consequences, rationale, owner and deadline. Routes are options (ATLAS optimise, REGULUS, COVENANT, VECTOR, VERITAS); nothing is executed or transmitted.
* **Closure** requires a fresh deterministic recheck made after submission for review that is **PASS**, plus a checker who did not author/submit the remediation. A later PASS observation never auto-closes a case.
* Waivers: one control, one exact subject (no wildcards), ≤ 30 days, internal/synthetic policy only (legal authority → refused), independent checker approval. They annotate cases; the raw evaluation status is never altered.
* SLA due = evaluation `as_of` + 4/24/72/120 h (CRITICAL/HIGH/MEDIUM/LOW). Overdue is computed against an explicit business `as_of`, never the server clock.
* Notifications are in-app only. Case history/comments are insert-only (no edit/delete code path). Concurrent updates: optimistic `expected_revision`; exactly one wins.

## 3. Surveillance (synthetic)
* Generator: 20 accounts × 240 days, per-account baselines, an expiry-week regime, injected anomaly episodes (1–3 days). **Labels are a separate object and never reach feature/normalisation code** (tests assert this).
* Features: signed exposure change, gross exposure, turnover, concentration, cancel ratio, price/volume deviation, margin utilisation, missing ratio. Expiry proximity is a normalisation regime.
* Normalisation: median/MAD per (account × regime) fitted on **train only**. Primary score = max |robust z|; challenger = IsolationForest (seed fixed) on |z|.
* Split is chronological 60/20/20 by day; the threshold is chosen on validation (max F1); metrics are reported on the later holdout.
* **Measured (seed 42, holdout, n = 960 account-days, 29 positives, prevalence 3.0 %)** — robust MAD: precision 1.000, recall 0.586 (17 TP, 12 FN, 0 FP), PR-AUC 0.804, 0.0 false alerts/1,000 obs, 9 of 13 injected episodes detected;
  IsolationForest challenger: recall 0.690, PR-AUC 0.907. These are properties of an injected synthetic benchmark, **not** evidence of real-world detection. Sample is small; differences between models are within noise.
* Anomalies open `ANOMALY_INVESTIGATION` cases titled “Investigation signal (not a finding)”. No evaluation row is created, so raw control status is unaffected (G22). A model can be DISABLED; a crude score-median drift ratio is reported (not a validated drift test).

## 4. Known limitations
* SQLite is used locally; PostgreSQL behaviour of the case/occurrence uniqueness and concurrency tests is **not** verified here.
* No frozen contract exposes positions, reconciliation status or allocation slacks to compliance; those inputs are caller-supplied (labelled) or UNSUPPORTED. Proposed shared change: `risk.get_positions`, `risk.reconciliation_status`, `collateral.constraint_slacks`.
* Control population (`CmpControl`) is a registry snapshot of rule revisions; automatic re-evaluation of affected accounts on rule activation (G23) is provided only as `supersede_control` (effective-dated revisions) — no event-driven re-evaluation job yet.
* Scheduled (DAILY) evaluation / missed-run detection (S01) is **not implemented**: only event-triggered evaluation exists. `cmp_jobs` records surveillance training only.
* Load: a single-process burst of 40 `evaluate()` calls (2 controls each) took ≈2 s on SQLite (≈20 events/s); no p50/p95 benchmark or rule-evaluation target (2,000 positions × 50 rules) was run.
