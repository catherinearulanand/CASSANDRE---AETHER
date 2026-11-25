# Frozen shared contracts v1 (lead-owned; change only by versioned proposal to the lead)

All amounts are **decimal strings**, currency is an ISO code from `app.core.money.SUPPORTED_CURRENCIES`. No floats cross a
service boundary. Every service call is `app.core.services.call(name, session, tenant_id, ...)` using the caller's open
DB session/transaction. Engines never import each other and never touch another engine's tables.

## Shared fixture IDs (all engines must use these exact IDs; tenant `tenant-a`)
- Entities: `ENT-MERIDIAN` (our bank, IN), `ENT-NORTHWIND` (counterparty/clearing member, IN), `ENT-NORTHWIND-IFSC` (IFSC).
- OTC: master `MA-MER-NWD-001`, CSA `CSA-MER-NWD-001`, netting set `NS-MER-NWD-001` (base USD); second master `MA-MER-OTH-002`, netting set `NS-MER-OTH-002`.
- ETD domestic: account `ACC-DEMO-NSE-001` (venue `DEMO_NSE`, INR); IFSC: `ACC-DEMO-IFSC-001` (venue `DEMO_IFSC`, USD).
- Market types: `OTC`, `ETD_DOMESTIC`, `ETD_IFSC`, `CCP_OTC` (unsupported => explicit UNSUPPORTED result).
- Snapshot IDs: `SNAP-QUIET-001`, `SNAP-SHOCK-001`, `SNAP-ETD-001`, `SNAP-IFSC-001`, `SNAP-STALE-001`.
- Rule keys: `DEMO-ETD-MARGIN` (synthetic margin multiplier policy), `DEMO-ETD-COVERAGE`, `DEMO-ETD-POSLIMIT`, `DEMO-OTC-VM`, `DEMO-IFSC-COVERAGE`.

## regulatory (A1)
`regulatory.resolve_rules(session, tenant_id, context: dict, as_of: datetime, known_at: datetime|None, hypothetical_rule_ids: list[str]|None=None) -> dict`
context keys: `market_type, venue, jurisdiction, entity_category, instrument_class, account_id, currency`.
returns `{"applicable":[{"rule_key","version","rule_hash","dsl","authority_type","source_spans","trace"}],"excluded":[...],"unknown":[...],"conflicts":[...],"rule_set_hash":str,"hypothetical":bool}`.
Missing context => rule in `unknown` (never silently applicable). Conflicts => non-empty `conflicts`. Hypothetical rules only when ids passed; result flagged `hypothetical:true`.
`regulatory.rule_parameters(session, tenant_id, rule_set_hash) -> dict` returns approved numeric parameters (e.g. margin multiplier) used by risk.

## contracts (A2)
`contracts.resolve_terms(session, tenant_id, netting_set_id, as_of, known_at, fact_snapshot_id=None) -> dict`
`{"status":"RESOLVED"|"BLOCKED","terms_hash":str|None,"blocking_reasons":[str],"source_ids":[...],"terms":{
 "party_a_id","party_b_id","base_currency","threshold_a","threshold_b","mta_a","mta_b","independent_amount_a","independent_amount_b",
 "rounding_increment","rounding_mode":"UP|DOWN|HALF_UP|HALF_EVEN","mta_semantics":"GTE|GT","segregated_im":bool,
 "eligible_schedule":[{"asset_class","currency","issuer_class","valuation_pct","haircut_pct","fx_haircut_pct","concentration_cap_pct","cap_denominator","min_rating","max_maturity_years"}]}}`
`contracts.netting_membership(session, tenant_id, trade_id, as_of) -> {"netting_set_id"|None,"master_id","reason"}` (unknown mapping => None + quarantine).

## risk (A3) — sole producer of margin results
`risk.compute_margin(session, tenant_id, context: dict, snapshot_id, terms_hash, rule_set_hash, methodology: str) -> MarginResult`
`risk.get_result(session, tenant_id, result_id) -> MarginResult` (immutable once stored).
MarginResult = `{"result_id","market_type","status":"OK|BLOCKED|UNSUPPORTED","blocking_reasons":[],"currency","account_id"|"netting_set_id",
 "exposure":{"amount","currency"},"required_amount":{"amount","currency"},"direction":"POST|RETURN|NONE","unrounded_amount","rounded_amount",
 "components":[{"kind":"VM|IM|IMPORTED","amount","currency"}],"waterfall":[{"step","value","citation"}],"input_hashes":[],
 "snapshot_id","terms_hash","rule_set_hash","method","method_version","result_hash"}`.
`risk.reconcile_clearing(session, tenant_id, file_id, snapshot_id)`, `risk.stress(session, tenant_id, base_result_id, shock_spec)`.
Stored via `risk_results`; reads by other engines only through `risk.get_result`.

## collateral (A4)
`collateral.optimise(session, tenant_id, margin_result_id, inventory_snapshot_id, rule_set_hash, terms_hash, deadline, horizon_days) -> proposal dict`
(`proposal_id,status,solver_status,legs[],objective,baseline,residual,exclusions[],constraints[],proposal_hash`).
`collateral.verify(session, tenant_id, proposal_id)`, `collateral.reserve(session, tenant_id, proposal_id, approval_id)`.
`collateral.coverage(session, tenant_id, account_id, as_of) -> {"eligible_posted":{"amount","currency"},"as_of"}` (posted only, never proposed).

## compliance (A5)
`compliance.evaluate(session, tenant_id, context, snapshot_id, rule_set_hash, margin_result_id=None, allocation_id=None) -> {"evaluations":[...],"cases":[...]}`
`compliance.recheck(session, tenant_id, case_id, snapshot_id)`.

## evidence (A6) — foundation already committed in `app/engines/evidence/service.py`
`evidence.append(session, tenant_id, *, engine, action, actor_id, actor_type, entity_type, entity_id, correlation_id, ...)` — call in the SAME
transaction as the domain write. `evidence.verify_chain`. A6 adds `evidence.store_object`, checkpoints, bundles, replay.
Actions are `UPPER_SNAKE` verbs; every engine must emit evidence for each consequential write (proposal, review, approval, activation, computation, allocation, control evaluation, case transition).

## Approvals (shared)
`core_approvals` (ApprovalRow). Use `app.auth.security.require_distinct_checker(maker_id, principal)`; bind `target_hash` + `target_revision`; expire via `expires_at`; reject if the target hash changed.

## Events
Use `app.core.events.emit(...)` for the catalogue in `EVENT_TYPES`; owner per `EVENT_OWNER`. Only the owner emits a type.

## Contract v1.1 additions (lead-accepted, ADDITIVE only)
- Evidence events for consequential writes SHOULD carry `payload.parents = [{"entity_type","entity_id"}, ...]` (lineage edges) and replayable
  computations carry `payload.replay = {"replayer":"replay.<engine>","engine_version","method_version","business_hash","inputs":{...},"values"?,"tolerance"?}`;
  each engine registers `replay.<engine>(session, tenant_id, spec) -> {"engine_version","method_version","business_hash","values"?}`. See `app/engines/evidence/replay.py`.
  The business hash excludes run IDs, timestamps and audit sequence.
- risk: `MarginResult` gains `as_of` and `snapshot_as_of` (ISO UTC); new services `risk.get_positions(session, tenant_id, account_id, as_of)` returning
  `{"gross_lots","net_lots","delta_equivalent_lots","notional","currency","client_class","as_of","source"}` and
  `risk.reconciliation_status(session, tenant_id, account_id, as_of)` returning `{"status":"OK|EXCEPTION|UNKNOWN","expected_total","imported_total","difference","currency","file_id"}`.
- collateral: `collateral.constraint_slacks(session, tenant_id, proposal_id)` returning per-constraint `{"name","limit","used","slack","binding"}`.
- Bitemporal: callers must pass `known_at` at/after a terms freeze when calculating with those terms.
- Concurrency rule (learned on PostgreSQL): every read-check-write mutation must lock the row (`lock_for_update` + `populate_existing`) before checking revision/state.
