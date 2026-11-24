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
