# COVENANT (A2) — Contract intelligence methodology

Scope: **bilateral OTC** agreement terms (Master / Schedule / CSA / Confirmation / Amendment) turned into direction-aware,
immutable, effective-dated, maker/checker-approved terms for VECTOR and ATLAS. Not for exchange-traded contract specs.
All documents are original, fictional and synthetic. Document metadata (parties, dates, hashes) is **not** an enforceability
or legal opinion.

## Pipeline
1. **Package** (`POST /contracts/packages`): parties, base currency, netting set, documents (immutable text, SHA-256).
   A netting set belongs to exactly one master (DB unique `(tenant, netting_set)`; second master => `CROSS_MASTER_NETTING_SET`).
   Document headers must agree with the package: `Party A:/Party B:` names (swapped parties rejected) and `Effective Date`.
2. **Extract** (`POST /contracts/{id}/extract`) — mode always recorded per field: `RULES` (deterministic regex/table extractor),
   `REPLAY` (hand-labelled fixture bound to the document SHA-256; modified bytes => `REPLAY_SOURCE_MISMATCH`).
   `LIVE_MODEL` => 501 (not implemented). No confidence score is produced or used. Every proposal is re-validated: the quote
   must reproduce from the stored text at the cited offsets; direction must match the party named in the quote; the value must
   pass the typed catalogue. Failures => status `UNRESOLVED` with issues; nothing is auto-corrected or approved.
3. **Review** (`POST /contracts/{id}/review`): per-field `APPROVE` (checker role, never the field's maker, even with both roles),
   `CORRECT` (maker; original extraction preserved in `original_value`), `REJECT`, `MARK_UNRESOLVED`. Reason mandatory,
   optimistic `expected_revision`. Tenant admin has no approval power.
4. **Freeze**: `freeze/propose` (maker) creates a `core_approvals` row binding package revision + candidate bundle hash;
   `freeze` (independent checker) re-derives the hash and rejects stale/expired/used approvals. Any field/doc change bumps the
   package revision and expires pending approvals. Frozen fields are read-only server-side; changes need an **amendment**, which
   goes through the same review and produces a new version that supersedes (never edits) the previous one.
5. **Resolve** (`contracts.resolve_terms`): picks the latest version with `frozen_at <= known_at` (recording time), evaluates
   entries at `as_of` (business time, inclusive boundary), returns `terms_hash` or `BLOCKED` + reasons.

## Field semantics
- Direction `A`/`B` = Party A / Party B of the package; `threshold_a` is the Threshold stated "with respect to Party A".
- Required to freeze: thresholds, MTAs, independent amounts (A and B), rounding increment & mode, MTA semantics (`GTE`|`GT`),
  netting authorization, segregated IM flag, eligible schedule. Missing => `MISSING_REQUIRED_FIELD:<key>` (no silent defaults).
  Zero thresholds/MTAs are valid; negatives and non-positive rounding increments are not.
- Money is `{amount: decimal string, currency}`; currency must equal the base currency (no implicit conversion).
  Decimal-separator ambiguity (`10.000,00`, `1.500`, `10,5`) is flagged `AMBIGUOUS_DECIMAL_SEPARATOR`, never guessed.
- Eligible schedule: `valuation_pct + haircut_pct = 100` (two views of one discount — never apply both); `fx_haircut_pct` is a
  separate additive reviewed factor; a concentration cap requires an explicit `cap_denominator`
  (`OBLIGATION` | `TOTAL_EFFECTIVE_ALLOCATED`). Duplicates, unsupported currency, unknown rating => whole schedule rejected.
- Limitation: one `rounding_mode` for both delivery and return amounts (directional rounding modes are not modelled yet).

## Conditional terms
Typed bounded predicate AST (`all`/`any`/`not`/leaf with ops eq,neq,lt,lte,gt,gte,in; rating scale AAA..D), three-valued.
A conditional entry needs external facts from a **fact snapshot** (explicit `source_id`, `effective_at <= as_of`: future facts are
ignored). Missing/off-scale facts => UNKNOWN => `UNRESOLVED_CONDITIONAL_FACT` (blocked), never PASS.

## Hierarchy (not "last document wins")
Among entries effective at `as_of`: conditional-TRUE entries compete with unconditional ones; winner = highest
`(document rank, effective_from, conditional-first)` with ranks Master 1 < Schedule 2 < CSA 3 < Confirmation 4 < Amendment 5.
Equal precedence with different values => `CONFLICTING_TERMS` (blocked). Regulatory limits (A1) are checked separately and are
**not** evaluated here (`regulatory_constraints: NOT_EVALUATED_HERE`).

## Hashes
`bundle_hash = H("aether.content.v1", package ids + sorted entries(key,direction,value,condition,effective_from,rank,doc_sha256,start,end))`
— independent of UUIDs. `terms_hash = H(bundle_hash, netting_set, resolved terms, facts_hash)`; resolved terms are stored in
`con_resolutions` so other engines can re-read exactly what was pinned (`contracts.terms_by_hash`).

## Evidence & events
Every consequential write appends an evidence event in the same transaction (`CONTRACT_PACKAGE_CREATED`,
`CONTRACT_DOCUMENT_ADDED`, `AMENDMENT_ADDED`, `CONTRACT_EXTRACTED`, `CONTRACT_FIELD_REVIEWED`, `TERMS_FREEZE_PROPOSED`,
`TERMS_APPROVED`, `TERMS_FROZEN`, `FACT_SNAPSHOT_RECORDED`, `NETTING_MAPPING_RECORDED`). Outbox events `TermsApproved`/`TermsFrozen`.

## Netting membership
`netting_membership(trade)` -> `MEMBER` only if the declared master equals the netting set's master **and** an approved netting
authorization exists. Declared-master mismatch => `REJECTED (CROSS_MASTER)`; unknown set/authorization missing => `QUARANTINED`.
Quarantined/rejected trades return `netting_set_id: null`. Remapping a trade is a conflict (reviewed amendment required).

## Limitations (explicit)
Regex extraction handles only the fictional clause grammar in `fixtures/contracts/documents`; no OCR/scanned documents; no
LIVE_MODEL; no extraction-accuracy claim from REPLAY; offsets are character offsets (equal to byte offsets for the ASCII fixtures,
flagged otherwise); signatories are not modelled; per-field maker is the user who ran extraction/last corrected.

---

# Round 2 additions

## Evidence contract (payload.parents / payload.replay / `replay.contracts`)
* **Lineage edges** (`payload.parents`): `contract_document → contract_field → approval → contract_terms` (the approval is its
  own `approval` node, so the APPROVAL stage is populated); `trade_mapping` and audited resolutions point at `contract_terms`.
* **Replay specs** (`payload.replay`, replayer `replay.contracts`, `engine_version covenant/2.0`, `method_version terms-resolution/1`):
  * `TERMS_FROZEN` → `inputs.kind = FROZEN_BUNDLE`: recompute the bundle hash from the **stored** version entries. A silent
    post-freeze edit of an entry makes the replay `DIVERGED` (tested by tampering a stored threshold).
  * `TERMS_RESOLVED` / `TERMS_RESOLUTION_BLOCKED` (written by `POST /contracts/resolve/record`) → `inputs.kind = RESOLUTION`:
    re-resolve at the pinned `as_of`, **`known_at`** and fact snapshot. `known_at` is pinned (defaults to "now") so a later
    amendment never changes what an old audited resolution reproduces (C-A08). A blocked resolution replays as the same block.
  * `bundle_hash` has one definition (`services._bundle_hash`) used by freeze **and** replay, so they cannot drift apart.
  * Bump `METHOD_VERSION` whenever resolution or hash semantics change; older events then replay as
    `NOT_REPLAYABLE (VERSION_MISMATCH)` rather than as a false `DIVERGED`.
* Tests: `test_contracts_replay_evidence.py` (MATCH, history reproducible after a later amendment, blocked resolution, tamper →
  DIVERGED, NOT_REPLAYABLE for missing / mismatched method version and absent replayer, lineage edges).

## Concurrency (PostgreSQL-found class)
Every read-check-write path is guarded: `get_package(lock=True)` row lock (review, extract, propose, freeze, amendment filing);
`create_package`, `register_trade`, `record_facts` use a SAVEPOINT + clean `Conflict` instead of a raw unique violation;
`register_trade` also locks the agreement row; `resolve_terms` inserts its content-addressed `con_resolutions` row with
`ON CONFLICT DO NOTHING`. `tests/engines/contracts/test_contracts_concurrency.py` uses real threads. **Each guard has a negative
control** (guard removed → its test fails on PostgreSQL): `reports/test-results/contracts_concurrency_negative_controls.json`.
The `register_trade` test needs a `before_flush` barrier (PostgreSQL only) to make the collision deterministic; without it the
window is too narrow to hit reliably. SQLite serialises writers, so these tests cannot demonstrate the race there.

## Column limits
PostgreSQL enforces `VARCHAR(n)`; SQLite does not. Over-long asset/issuer classes, percentages, maturities, identifiers and
document references are now validation errors (`UNRESOLVED` field / `ValidationFailed`), and a schedule that failed validation is
never materialised into `con_eligibility` (`test_contracts_column_limits.py`; the raw `DataError` was reproduced first).

## Extraction evaluation (spec 13.1) — `DETERMINISTIC_EXTRACTOR`, not `LIVE_MODEL`
Harness `tests/engines/contracts/eval_extraction.py`; output `reports/test-results/contracts_extraction_eval.json`. Documents
(`fixtures/contracts/eval/{docs,heldout}`) and gold (`fixtures/expected/contracts/eval_gold*.json`) were hand-written, independent of
the extractor. **No model accuracy is claimed**; these are regex-extractor results on 26 small synthetic documents.

| | Baseline (dev set, before hardening) | Dev set after hardening (optimistic: tuned on it) | Held-out (8 docs) |
|---|---|---|---|
| gold fields (normal / must-flag) | 99 / 7 | 99 / 7 | 38 / 4 |
| exact match after normalisation | 83/99 = 83.8 % [75.4, 89.8] | 91/99 = 91.9 % [84.9, 95.9] | 31/38 = 81.6 % [66.6, 90.8] |
| silent wrong clean value | 0 | 0 | 0 |
| silent omission (normal / must-flag) | 9 / 2 | 0 / 0 | 0 / 0 |
| non-operative clause proposed unflagged | 2 | 0 | 1 |
| unresolved-material-field recall | 5/7 = 71.4 % | 7/7 | 4/4 |
| citation recall / precision | 89.6 % / 93.8 % | 100 % / 100 % | 100 % / 100 % |
| scope accuracy (direction + condition) | 93.7 % | 100 % | 100 % |
| structurally invalid output rate | 8.3 % | 1.9 % | 0 % |

The 8 non-exact fields on the dev set and 7 on the held-out set are deliberately **surfaced** as `unparsed_clause` items (a
reviewer must key them in or reject them); they are not wrong values. Wilson 95 % intervals (in the JSON) are wide because the
samples are small. `contracts_extraction_eval.heldout_first_measurement.json` preserves the held-out result before any change it motivated.

**Weaknesses, stated plainly**
1. The "non-operative text" detector is a keyword heuristic: an "obsolete / no longer applies" paragraph (held-out H02) is still
   proposed as a clean value. It would collide with the real term at freeze (`CONFLICTING_TERMS`), but a reviewer could approve the wrong one.
2. Only 1 of 3 held-out prompt-injection markers was surfaced for review; the others are inert (never an operative field, never
   executed) but not flagged.
3. The held-out set is not fully unseen: HTML-comment detection was added after H03.
4. Table-form thresholds/MTAs, paraphrased prose, "5 million", space/apostrophe digit grouping and non-ISO rating scales are
   surfaced, not parsed.
5. One gold label (E16) was widened after the first run; recorded in the gold file's `revisions`.
6. Reviewer correction time was not measured (no reviewer study).

## Hardening (G28 and edge cases)
Instruction-shaped text (plain imperative, polite "dear assistant", fenced "system", JSON role blob) is surfaced as an UNRESOLVED
`unparsed_clause` flagged `SUSPECTED_PROMPT_INJECTION`; it can never be approved, creates no approval and no tool/freeze side effect
(asserted through the real service path), and freezing is blocked until a checker rejects it. Clause-shaped text in an HTML comment
or in a block marked illustrative/hidden is flagged `NON_OPERATIVE_CONTEXT`. `Party A` / `Party B` aliases resolve via the header;
a mislabelled direction is refused (`DIRECTION_MISMATCH`); zero is a valid explicit amount while negative/garbage is never clean;
the MTA boundary (`GTE` vs `GT`) and its amount flow unchanged into resolved terms. Schedule columns are located by header name.

## UI / accessibility pass
`tests/e2e/contracts_a11y.py`: axe (wcag2a/aa/21aa + best-practice) on every tab and the open review panel, keyboard, 390 px reflow.
Fixed: WAI-ARIA tabs (roving tabindex, Arrow/Home/End, `aria-controls`, panel `aria-labelledby`); accessible names containing the
visible value on the directional-term buttons; keyboard-focusable, named scroll regions. A workflow defect found by the browser
flow: the independent checker could not discover the maker's pending freeze approval (now `readiness.pending_approval`).

## Performance (PostgreSQL 16 in Docker, 120 agreements, shared machine)
`reports/benchmarks/contracts_pg.json` (machine load recorded in the file; other workers were running, so indicative only).
The registry listing was an N+1: 635 ms p50 → 33 ms p50 after batching (`contracts_pg_before_registry_fix.json` kept).
`resolve_terms` warm ≈ 8 ms p50.

## Proposal: one canonical vocabulary for the eligibility schedule (NOT implemented; needs an explicit contract decision)
ATLAS currently aliases `GOVERNMENT_BOND→GSEC`, `CORPORATE_BOND→CORP` and `TOTAL_EFFECTIVE_ALLOCATED→TOTAL_ALLOCATED`, and treats
`ANY` as an explicit wildcard. Proposal: freeze the **inventory** names as canonical in `terms.eligible_schedule`
(`asset_class ∈ {CASH, GSEC, CORP, …}`, `cap_denominator ∈ {OBLIGATION, TOTAL_ALLOCATED}`, `issuer_class` a free value or `ANY`);
COVENANT normalises at extraction (original text kept in `original_value`) and flags unknown classes UNRESOLVED instead of accepting
free text. This changes resolved terms and therefore `terms_hash`, so it must ship with `METHOD_VERSION → terms-resolution/2`
(old audited resolutions then replay as `NOT_REPLAYABLE: VERSION_MISMATCH`, not as false divergence) and with ATLAS removing its alias map in the same change.
