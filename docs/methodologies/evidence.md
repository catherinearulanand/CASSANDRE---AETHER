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

## 4. Replay contract (proposal to engine owners; implemented on the evidence side)
A replayable evidence event carries `payload["replay"] = {replayer, engine_version, method_version, business_hash, inputs, [values, tolerance]}` and the engine registers `services.register("replay.<engine>", fn)` returning `{engine_version, method_version, business_hash, [values]}`. The business hash must exclude run IDs, timestamps and audit sequence. Evidence runs the replayer inside a SAVEPOINT that is **always rolled back**, so replay cannot create reservations or any write. Status values: `MATCH`, `MATCH_WITHIN_TOLERANCE` (only with a declared tolerance on platform-dependent numerics), `DIVERGED` (first divergence reported), `NOT_REPLAYABLE` (missing version, unregistered replayer, or version drift — the current version is never silently substituted), `NOTHING_TO_REPLAY`. Cached REPLAY-mode extractions are reported with a caveat: replay of a cache is not generative determinism. **No engine currently emits `payload.replay`**, so on the integrated app replay reports `NOTHING_TO_REPLAY` until engine owners adopt the contract.

## 5. Lineage
Nodes come from event `entity_type/entity_id`, `payload.parents` (list of `{entity_type, entity_id}`), `source_spans` (`document_id`, offsets) and explicit `evidence.record_lineage` edges. Stages: SOURCE → SPAN → RULE_TERMS → SNAPSHOT → METHOD → MARGIN → PROPOSAL → CONTROL → APPROVAL. Missing stages and parents that were referenced but never recorded are returned in `missing`; `complete` is true only with none. Engines must populate `payload.parents` for the graph to be meaningful (proposal to engine owners).

## 6. Operations
* **Outage (G24):** `append` raises `Unavailable(EVIDENCE_UNAVAILABLE)` while evidence cannot be recorded; because engines append in the same transaction as the domain write, the transition is rolled back. Fault injection (`set_storage_outage`) is for tests/drills only and is not reachable over HTTP.
* **Retention:** per-category config; `retention_days = null` means unconfigured. No statutory period is invented. `EVIDENCE_EVENTS` are never deleted by the application; only `TEMP` vault objects can be purged (blocked by legal hold).
* **Access logging:** reads/exports are logged to `ev_access_log` (deliberately not in the chain, to avoid recursive self-audit loops).
* **Backup/restore:** SQLite only (consistent copy via sqlite3 backup API). Restore refuses a non-empty target, verifies every file hash, then every tenant chain against all signed checkpoints and every vault object before reporting success. PostgreSQL (`pg_dump`) path and backup encryption are **not implemented/tested**.

## 7. Gates (evidence in `tests/engines/evidence/`)
E-A01 helper only (`evidence.check_expected_actions`; lead verifies all six engines) · E-A02 `test_modify_reorder_remove_detected`, `test_removed_middle_event_is_a_gap` · E-A03 `test_truncation_detected_against_exported_signed_checkpoint`, `test_forged_checkpoint_file_rejected` · E-A04 `test_concurrent_append_valid_sequence_and_anchor` · E-A05/E-A06 `test_ev_replay_lineage.py` (replay semantics proven with test-injected replayers, **not yet against real engine replayers**) · E-A07 `test_ev_bundle_verifier.py` · E-A08 `test_ev_api.py` · E-A09 `test_ev_backup_restore.py` · E-A10 backend flow `test_ev_api.py` + real-server smoke; browser E2E **not run** (no Playwright/browser available).
Benchmarks: `reports/benchmarks/evidence.json` (SQLite, single noisy shared machine; see its `earlier_run_not_discarded` block).
