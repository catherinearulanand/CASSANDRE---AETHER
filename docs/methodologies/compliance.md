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
* Event-triggered: only control families whose inputs arrived are evaluated (`full_sweep`/`control_keys` override). Untriggered controls stay visibly *unevaluated*; controls whose inputs no engine provides are registered UNSUPPORTED (listed with the reason, never evaluated, never counted as passing). After round 2 all registered controls are supported.
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

## 4. Round 2 additions (contract v1.1)
### 4.1 Inputs now come from the owning engines
* Position limit: `risk.get_positions(account, as_of)`. The control declares `required_basis = GROSS_FUTURES_EQUIV`; the explicit mapping (`BASIS_MAP`) takes the service's **`gross_lots`**.
  No other basis is mapped (a control needing delta-equivalent/notional would be UNKNOWN until a mapping is reviewed). The service reports `client_class = "UNKNOWN"` because **no engine owns client classification**:
  that is treated as a *missing input* (scope UNKNOWN => control UNKNOWN, with the reason `CLIENT_CLASSIFICATION_MISSING`), never as a value and never as NOT_APPLICABLE.
  A caller may supply the classification; it is labelled `CALLER_SUPPLIED` and only fills a gap the service left.
* Reconciliation: `risk.reconciliation_status` (`OK` -> MATCHED, `EXCEPTION` -> FAIL with the difference, `UNKNOWN` -> UNKNOWN).
* Freshness: `input_as_of` = the **oldest** timestamp among all inputs used (risk `snapshot_as_of` preferred over `as_of`; positions; reconciliation file). A fresh calculation over a stale snapshot is stale.
* A margin result belongs to one subject: if `risk.get_result` names another account the control is UNKNOWN (`MARGIN_RESULT_SUBJECT_MISMATCH`), never silently evaluated.
* Hypothetical (what-if / draft-rule) margin results are flagged on the evaluation, evaluated into their **own** case (`[HYPOTHETICAL]`, separate occurrence key) and counted separately (`hypothetical_evaluations`); they never enter the official status counts.
### 4.2 Newly supported controls (all SYNTHETIC_POLICY)
| control | input | outcome rule |
|---|---|---|
| `CMP-COLL-CONC` | `collateral.constraint_slacks(proposal)` | FAIL if any constraint slack < 0 (zero slack is within the cap); UNKNOWN when unavailable |
| `CMP-SETTLE-DEADLINE`(INR) / `-USD` | `collateral.coverage(account, settlement_deadline)` | **posted** collateral as of the deadline >= required margin; a proposal/reservation is never counted; the as-of coverage control still FAILs (no early credit) |
| `CMP-REPORT-SCHEMA` | caller-supplied `report_record` | field presence + format (ISO currency, non-negative decimals, ISO-8601 with zone) against a **synthetic** demo schema. Not an approved regulatory format; nothing is transmitted |
The round-1 UNSUPPORTED rows for these keys are retired (`active=false`, never deleted). The UNSUPPORTED mechanism remains and is tested (a control without inputs is listed with its reason and never evaluated).
### 4.3 Scheduled evaluation (S01) and rule changes (G23)
* `schedule.py`: slot = UTC business date of the **supplied** `as_of` (no server clock). A slot is due at 12:00Z + 2 h grace (fictional close). `run_sweep` evaluates every DAILY SUPPORTED control for the known population
  and writes a `SCHEDULED_SWEEP` job with a watermark. `schedule_status` lists due/completed/missed slots and a stale-watermark flag; `record=true` writes one `MISSED_RUN_DETECTED` evidence event per newly missed slot (idempotent).
  An empty alert list is never presented as compliance: a stale watermark or missed slot says the controls were **not evaluated**.
* `POST /compliance/controls/{key}/supersede` (compliance officer/admin): new immutable revision, old gets `effective_to`; **only subjects previously evaluated by that control are re-evaluated**, from fresh inputs of the owning engines
  as of the effective date; other controls and other subjects are untouched; old evaluations/cases stay. A `RuleActivated` outbox event re-evaluates only controls bound to that `rule_key` (older pinned revisions then evaluate UNKNOWN `CONTROL_STALE_VS_ACTIVE_RULE`).
### 4.4 Evidence contract
* Entity types follow evidence.md 4.1: `cmp_evaluation`, `cmp_case`, `cmp_control`, `cmp_waiver`, `cmp_job`, `cmp_model`. `payload.parents`: evaluation -> `risk_result` (margin result id), `col_proposal` (allocation id), `reg_rule` (when the regulatory service names the rule id); case events -> `cmp_evaluation`.
* `CONTROL_EVALUATED` carries `payload.replay = {replayer: "replay.compliance", engine_version, method_version, business_hash, inputs: {evaluation_id}}` and `output_hash`. `replay.compliance` re-runs the **pure** evaluation from the persisted control revision, facts, stored input reasons and rule state
  (read-only, no evidence/outbox writes). Business hash = canonical hash of `{status, reasons, measured, limit, gap, unit}`; ids and timestamps are excluded. Tested: MATCH, DIVERGED when a persisted fact is altered, NOT_REPLAYABLE when the replayer is unregistered.
### 4.5 Concurrency (lock-before-check)
Every read-check-write path locks the row (`lock_for_update` + `populate_existing`) or relies on a unique key with a savepoint-protected insert: case occurrence (create/update), evaluation `eval_key`, case transition/dismissal/remediation/recheck, waiver decision, model status (no-op flips write no evidence), model version allocation (retry on conflict).
Six real-thread regression tests (`test_cmp_race_conditions.py`) were each run **with the guard removed** and fail (`reports/test-results/compliance_mutation_check.txt`).

## 5. Known limitations
* No engine owns client/proprietary/member classification: the position-limit control is UNKNOWN for every real account until one does (or a caller supplies a labelled value).
* The sweep re-uses each subject's last pinned snapshot/margin-result ids; it does not create new margin runs. Stale inputs therefore evaluate UNKNOWN until risk/collateral produce fresh ones.
* The report-schema control validates a **synthetic** schema only; there is no approved reporting schema and no submission.
* Performance and host-load figures: see `reports/benchmarks/compliance_pg.json` (shared development host, not a clean-room measurement).
