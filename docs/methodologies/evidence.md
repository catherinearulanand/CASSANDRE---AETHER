# A6 VERITAS — Evidence and Audit methodology (P0)

**Synthetic data only. Internal evidence packs are not regulatory submissions. Nothing here is certified, an HSM, or legal non-repudiation.**

## 1. What is built
| Capability | Where |
|---|---|
| Append-only hash-chained events, per-tenant chain-head lock, same-transaction append | `service.py` (lead foundation, extended) |
| Content-addressed object vault (write-once, tenant-scoped path, re-hash on read) | `objects.py` |
| Ed25519 signed checkpoints, exported **outside the DB** | `keys.py`, `checkpoints.py` |
| Signed audit bundles (zip: manifest, signature, events JSON+CSV, chain skeleton, lineage, replay, objects) | `bundle.py` |
| Independent offline verifier (no platform imports) | `tools/verify_bundle/` |
| Lineage graph with explicit missing links | `lineage.py` |
| Read-only replay (NOT_REPLAYABLE when versions absent) | `replay.py` |
| Search, access log, retention config, integrity dashboard | `ops.py` |
| API (no generic append endpoint) | `api.py` |
| Backup / restore with anchor verification | `backend/scripts/backup.py`, `restore.py` |

## 2. Canonical record and hash
UTF-8, sorted keys, no whitespace, NFC strings, Decimal as normalised plain string, floats and naive datetimes rejected, datetimes `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
`event_hash = SHA-256("aether.evidence.event.v1" ‖ 0x00 ‖ canonical({chain_id=tenant, sequence, prev_hash, every business field}))`.
Golden value reproduced independently: `fixtures/expected/evidence/golden_event.json` (generated with `hashlib`+`json` only), asserted against both the platform and the standalone verifier in `tests/engines/evidence/test_ev_golden_oracle.py`.

## 3. What integrity claims are (and are not) made
* The chain detects modification, reordering, gaps and a stored-head mismatch.
* A bare chain **cannot** detect privileged tail truncation (test `test_truncation_detected_against_exported_signed_checkpoint` proves the bare check passes after truncation).
* Truncation/rewrite is detected only against a **signed checkpoint exported outside the DB** (`<signing_key_dir>/../checkpoints/<tenant>/`). A forged/edited checkpoint file fails signature verification and is not trusted.
* The signing key is a **local file key** (`ev-<key_id>.pem`, chmod 600 where supported). It is not an HSM. Anyone with filesystem access can sign. Rotation: `keys.rotate()` creates a new key; old `.pub` files stay for verification. Recovery: restore the key directory from a separately protected backup; the private key is never in `backup.py` output.
* A bundle verification proves: files match what the key-holder signed; included events recompute to their hashes; each belongs to the signed chain prefix. It does not prove the operator did not hold the key, completeness beyond the checkpoint, or admissibility.
* Redacted bundles omit event content, prove chain membership only, and are marked `redacted: true` with a note that they do not contain the whole chain.

## 4. Replay and lineage contract (v1.1, binding on engines; shape is frozen, only additions allowed)

### 4.1 Lineage: `payload.parents`
Every evidence event for a consequential write SHOULD carry `payload["parents"] = [{"entity_type": str, "entity_id": str}, ...]` naming the
recorded entities the event's own `entity_type/entity_id` was derived from. Rules:
* `entity_type`/`entity_id` of the event is the **child**; each parent becomes an edge parent -> child.
* A parent must itself be the `entity_type/entity_id` of some recorded event in the same tenant. Identity is by **canonical type**: `margin_result` and `margin`
  are aliases of `risk_result` (same id = same node). A parent NOT recorded under the decision's own correlation id (seed-time snapshots, earlier approvals) is looked up
  across the tenant; if found it is included, flagged `external: true`, and ITS parents are followed (depth <= 6, <= 300 nodes). Only a parent with no event anywhere in the
  tenant is reported as `PARENT_NOT_RECORDED` (never hidden). Ids are strings; use the engine's stable id (`risk_result.result_id`, rule id, terms hash, ...).
* The graph for a decision is built per `correlation_id`. Engines that compute inside a run must reuse the run's correlation id; seed-time or
  cross-run parents are connected with `evidence.record_lineage(session, tenant, correlation_id, parent_type, parent_id, child_type, child_id)`.
* Stage mapping (SOURCE, SPAN, RULE_TERMS, SNAPSHOT, METHOD, MARGIN, PROPOSAL, CONTROL, APPROVAL) is by `entity_type`. Recognised today:
  `reg_source, contract_document` (SOURCE) · `reg_span, source_span` (SPAN) · `reg_rule, contract_terms, contract_package, contract_field, rule_set` (RULE_TERMS) ·
  `risk_snapshot, risk_clearing_file, risk_import, col_snapshot, fact_snapshot, trade_mapping` (SNAPSHOT) · `reg_impact_run, method` (METHOD) ·
  `risk_result, risk_stress` (MARGIN) · `col_proposal, col_instruction` (PROPOSAL) · `cmp_evaluation, cmp_case, cmp_control` (CONTROL) · `approval, cmp_waiver` (APPROVAL).
  Unknown types fall back by prefix (`cmp_`, `col_`, `risk_`, `reg_`, `contract_`), else `OTHER` (kept in the graph, not counted as a stage).
* `source_spans` on an event (`{document_id|source_id, span_id?, start|char_start, end|char_end}`) also create SOURCE and SPAN nodes automatically.
* `complete` is true only when all nine stages have at least one recorded node and no referenced parent is unrecorded. A flow that legitimately has no
  stage (e.g. ETD has no bilateral SOURCE terms) is reported as incomplete for that stage; this is intentional, the UI shows it explicitly.

### 4.2 Replay: `payload.replay` and `replay.<engine>`
A replayable event (a deterministic computation, not a human decision) carries
`payload["replay"] = {"replayer": "replay.<engine>", "engine_version": str, "method_version": str, "business_hash": str, "inputs": {...}, "values"?: {k: decimal-string}, "tolerance"?: decimal-string}`
and the engine registers `services.register("replay.<engine>", fn)` with
`fn(session, tenant_id, spec) -> {"engine_version": str, "method_version": str, "business_hash": str, "values"?: {...}}`.
* `spec` is exactly the recorded `payload["replay"]` dict. `inputs` must be **persisted references** (ids/hashes of immutable inputs, e.g. snapshot id,
  terms hash, rule_set_hash, inventory snapshot id, margin result id), never live data and never the current clock; any as-of/known-at used must be inside `inputs`.
* The replayer **recomputes** from those persisted inputs using the engine's *current code* and returns the version strings that code reports.
  If they differ from the recorded versions the replay is `NOT_REPLAYABLE (VERSION_MISMATCH)`; the current version is never silently substituted.
* The **business hash** = `evidence`-independent canonical hash (use `app.core.canonical.content_hash`) of the business outcome ONLY. Exclude: run/result/proposal ids
  generated at compute time, timestamps, audit sequence numbers, `created_at`, correlation ids. Include: amounts (decimal strings), direction, status, legs, solver status,
  rule/terms/snapshot hashes. Two computations of the same inputs must hash identically.
* Platform-dependent numerics (LP solver values) may declare `tolerance` and put the compared decimals in `values`; then a hash mismatch is `MATCH_WITHIN_TOLERANCE` only if every value is within the tolerance, else `DIVERGED` with the first divergence.
* Replayers MUST be read-only and MUST NOT call `evidence.append`, `emit`, reserve inventory or persist results. Evidence runs them inside a SAVEPOINT that is always rolled back
  as a safety net, but a replayer that depends on writes is not replayable. A replayer that needs a stored result should read it (e.g. `risk.get_result`) and recompute.
* Human decisions (approvals, reviews) are not replayed; they are lineage nodes. Events with no `payload.replay` are skipped; a run with none reports `NOTHING_TO_REPLAY`.
* Statuses: `MATCH`, `MATCH_WITHIN_TOLERANCE`, `DIVERGED` (first divergence reported), `NOT_REPLAYABLE` (missing version, unregistered replayer, replayer error, version drift), `NOTHING_TO_REPLAY`.
  Cached REPLAY-mode extractions (regulatory/contracts) carry a caveat: replaying a cache is not generative determinism.

## 5. Lineage (implementation notes)
Nodes come from event `entity_type/entity_id`, `payload.parents`, `source_spans` and explicit `evidence.record_lineage` edges; see 4.1 for the binding contract.
The deep links `/evidence?run=<run_id>` and `/evidence?entity=<entity_id>` resolve to a correlation id (a run id maps to its `correlation_id`; an entity id is looked up
across `ev_events.entity_id`/`payload.parents`, scoped to the caller's tenant, never revealing other tenants).

## 6. Operations
* **Outage (G24):** `append` raises `Unavailable(EVIDENCE_UNAVAILABLE)` while evidence cannot be recorded; because engines append in the same transaction as the domain write, the transition is rolled back. Fault injection (`set_storage_outage`) is for tests/drills only and is not reachable over HTTP.
* **Retention:** per-category config; `retention_days = null` means unconfigured. No statutory period is invented. `EVIDENCE_EVENTS` are never deleted by the application; only `TEMP` vault objects can be purged (blocked by legal hold).
* **Access logging:** reads/exports are logged to `ev_access_log` (deliberately not in the chain, to avoid recursive self-audit loops).
* **Backup/restore:** SQLite only (consistent copy via sqlite3 backup API). Restore refuses a non-empty target, verifies every file hash, then every tenant chain against all signed checkpoints and every vault object before reporting success. PostgreSQL (`pg_dump`) path and backup encryption are **not implemented/tested**.

## 7. Gates (evidence in `tests/engines/evidence/`)
E-A01 helper only (`evidence.check_expected_actions`; lead verifies all six engines) · E-A02 `test_modify_reorder_remove_detected`, `test_removed_middle_event_is_a_gap` · E-A03 `test_truncation_detected_against_exported_signed_checkpoint`, `test_forged_checkpoint_file_rejected` · E-A04 `test_concurrent_append_valid_sequence_and_anchor` · E-A05/E-A06 `test_ev_replay_lineage.py` (replay semantics proven with test-injected replayers, **not yet against real engine replayers**) · E-A07 `test_ev_bundle_verifier.py` · E-A08 `test_ev_api.py` · E-A09 `test_ev_backup_restore.py` · E-A10 backend flow `test_ev_api.py` + real-server smoke; browser E2E **not run** (no Playwright/browser available).
Benchmarks: `reports/benchmarks/evidence.json` (SQLite, single noisy shared machine; see its `earlier_run_not_discarded` block).
