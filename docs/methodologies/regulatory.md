# REGULUS (A1) — Regulatory intelligence: methodology and limitations

All sources, rules and numbers in this build are **synthetic / fictional policy**. Nothing here is legal advice, a
statement of current law, or a regulatory approval. Real public circulars may be ingested as `AUTHORITATIVE`
sources, but they stay `UNVERIFIED` until a human review process (outside this build) says otherwise, and no real
obligation has been encoded.

## 1. Pipeline
`source → validated ingestion → spans → candidates (proposals) → draft rule → compile + test vectors → review → approve → activate → resolve`

* **Ingestion (R01)**: magic-byte sniffing (PDF / HTML / UTF-8 text) cross-checked against the declared content type, size
  limit (`AETHER_MAX_UPLOAD_BYTES`), PDF page limit (200), encrypted/unreadable PDFs rejected, NUL bytes rejected,
  deduplication by SHA-256 (idempotent). Original bytes and extracted text are stored separately; pages are joined by
  `\f`. PDF pages with no text layer are recorded in `unreadable_pages` and **block extraction** (no silent inference,
  no OCR adapter configured). HTML text is visible text only (script/style dropped, no entity/DTD resolution).
  Authority classes: `AUTHORITATIVE, DRAFT_CONSULTATION, INDUSTRY_STANDARD, INTERNAL_POLICY, SYNTHETIC, SUPERSEDED`.
* **Spans / grounding (R02, R-A01)**: a span stores character offsets into the stored text, the page, the exact quote and —
  for plain-text documents — UTF-8 byte offsets into the original bytes. `validate_span` re-checks quote == text slice,
  quote == original-byte slice and source-bytes hash on every review/activation, so tampering with stored text or
  bytes is detected. Quote location is exact-match only (one occurrence, else `QUOTE_NOT_FOUND` / `AMBIGUOUS_QUOTE`).
* **Extraction modes (labelled on every candidate)**: `REPLAY` (hand-labelled fixture keyed by the SHA-256 of the exact
  source bytes — **not a model run, confers no accuracy claim**), `DETERMINISTIC` (regex obligation finder that flags
  `AMBIGUOUS_UNIT`, `SCOPE_GAP`, `UNRESOLVED_REFERENCE:…`, `CONTRADICTION`), `MANUAL` (human-supplied), `LIVE_MODEL`
  (**disabled: returns 409, no silent fallback**; adapter not implemented). `confidence_category` names the method
  and is never an accuracy figure. Extraction never creates a rule.
* **Untrusted text**: documents are data. Instruction-like text is flagged (`risk_flags`, `SOURCE_HAS_INJECTION_PATTERNS`)
  but no code path takes an action from document text; extraction calls no other engine (tested with tripwire services).

## 2. Rule DSL (§4.1)
JSON AST; boolean ops `all any not eq in gt gte lt lte exists between effective_on`; value nodes `field`, bare registry
field names, `const{unit}`, `param`, `abs`, `ratio`, `sum_by`. No `eval`, no user code, no regex, no arbitrary expressions.
* **Registry** (`GET /regulatory/field-registry`) fixes type, **unit** and **sign convention** per field. Decimal
  comparisons require identical units; numeric constants without a valid unit do not compile (G29); floats are rejected.
  The subject (first) operand of a comparison must be a registry field (typos do not become string literals).
* **Three-valued evaluation**: TRUE / FALSE / UNKNOWN; missing or unparseable context is UNKNOWN; `all`/`any`/`not` follow
  Kleene logic (property-tested: De Morgan holds); `exists` is two-valued; division by zero is UNKNOWN. UNKNOWN is never PASS.
* **Bounds**: depth ≤ 8, nodes ≤ 200, steps ≤ 20,000, rows ≤ 100,000, wall clock ≤ 0.5 s; overrun ⇒ UNKNOWN with a trace entry.
* Scope and condition are compiled and evaluated separately. A1 evaluates **scope** (applicability). The **condition** is
  returned for the control engine (A5). `resolved_dsl` returns the AST with `{"param":…}` inlined as `{"const","unit"}`.

## 3. Lifecycle, governance, bitemporal registry (R04)
`DRAFT → REVIEW_REQUIRED → APPROVED → SCHEDULED|ACTIVE → SUPERSEDED | WITHDRAWN`
* Submit needs: compiles, ≥1 cited span that re-validates, source authority compatible with the rule's authority
  (`DRAFT_CONSULTATION`/`SUPERSEDED` sources are never a binding basis; `REGULATORY_VERIFIED` needs `AUTHORITATIVE`),
  ≥1 test vector, all vectors passing. A rationale/reason is mandatory at every step.
* **Maker ≠ checker**: only the author submits/edits drafts; approval by the author is `403 SELF_APPROVAL`, a non-checker
  `403 CHECKER_ROLE_REQUIRED`; **the denial is committed as an `APPROVAL_DENIED` evidence event before the 403** (G15).
* Approval binds `(rule, revision, content_hash)` in `core_approvals`, has a validity window (`AETHER_APPROVAL_TTL_SECONDS`),
  and expires. Any content change after approval ⇒ `APPROVAL_HASH_MISMATCH`. Rejection returns a revisable DRAFT; approved
  content is never edited; **rollback/amendment = a new version** (`/revise`), never a rewrite.
* **Activation is an explicit human act.** A future `effective_from` yields `SCHEDULED` (never ACTIVE early). A persisted
  `SCHEDULED→ACTIVE` flip happens only through `POST /rules/advance` with an explicit clock; reads show `effective_state`.
  A new version must take effect strictly after the version it supersedes (`OVERLAPPING_REVISION` otherwise).
* **Bitemporal resolution**: for each `rule_key`, the highest version that was *recorded* (activated) by `known_at`, has
  `effective_from ≤ as_of`, and is not withdrawn as of `known_at` governs, provided `as_of < effective_to`. Therefore an
  amendment applies at its exact effective instant and replays known earlier are unchanged (R-A04). `known_at=None` means
  "latest knowledge". Fixture rules use a fixed epoch recording time so scenario replays are deterministic.

## 4. Resolution (R03) — `regulatory.resolve_rules`
Outputs `applicable / excluded / unknown / conflicts / trace`, `rule_set_hash` (pinned immutably in `reg_rule_sets`),
`hypothetical`, `official`.
* Scope TRUE ⇒ applicable; FALSE ⇒ excluded; UNKNOWN (e.g. missing `entity_category`, R-A06) ⇒ `unknown` with the missing fields.
* Rules sharing a `conflict_group`: a different **approved** `precedence_rank` orders them (lower wins; never model-chosen);
  `additive_stricter` rules coexist; equal rank with differing values of the same parameter ⇒ `conflicts`, and **neither applies** (R-A05).
* `hypothetical_rule_ids` overlay counterfactual versions (replacing the in-force version of the same key); the result is
  flagged `hypothetical:true, official:false` and its rule-set hash differs from the official one (R-A09).
* `regulatory.rule_parameters(rule_set_hash)` returns pinned parameters per rule plus a **flat view** of unambiguous
  parameter names (e.g. `margin_multiplier`) because the risk engine reads that scalar; ambiguous names are listed, not flattened.

## 5. Impact analysis (R05)
For each account: resolve the baseline and counterfactual rule sets on the same `as_of`/`known_at`; **identical rule-set hash ⇒
not recomputed** (dependency index). Otherwise `risk.compute_margin` is called for both hashes (A3 does all margin arithmetic;
A1 only subtracts the two returned `required_amount`s in Decimal, same currency, else BLOCKED) and `collateral.coverage` supplies
eligible **posted** collateral for the shortfall (`max(0, required − posted)`). A missing or blocking service ⇒ that account is
`BLOCKED` with the service's reason, nothing is estimated, unknowns are counted and never aggregated as zero, aggregates are per
currency. Runs are always hypothetical (`official:false`, `publishes_compliance_status:false`). **`compliance.evaluate` is deliberately
not invoked for counterfactuals** (it would create operational cases); affected controls are reported as the changed rule keys.
Liquidity feasibility (`collateral.optimise` what-if) is **not wired** in this build and is stated as such in every result.

## 6. Evidence and events
Every consequential write appends a hash-chained evidence event in the caller's transaction: `SOURCE_INGESTED`,
`CANDIDATES_EXTRACTED`, `RULE_DRAFTED/_DRAFT_EDITED/_SUBMITTED/_APPROVED/_REJECTED/_ACTIVATED/_SCHEDULED/_SUPERSEDED/_WITHDRAWN/
_BECAME_EFFECTIVE`, `APPROVAL_DENIED/_EXPIRED`, `IMPACT_RUN_COMPLETED`. Outbox events: `DocumentIngested, RuleProposed, RuleApproved,
RuleActivated, RuleSuperseded` (A1 is the sole owner). `ApprovalRecorded` belongs to the platform and is not emitted here.
`rule_parameters`/`resolve_rules` pin a rule-set snapshot (idempotent insert) without an evidence event, because they are reads.

## 7. Known limitations
No OCR; no live model; no real regulatory source has been encoded or verified (register in spec §17 is a research baseline only);
precedence is only as good as the human-approved `precedence_rank`; no legal-interpretation workflow beyond maker/checker;
REPLAY accuracy cannot be claimed; the DEMO tier has not been benchmarked for this engine; `sum_by` positions rows are
accepted only from callers (A5/A3 supply them); condition evaluation lives in A5.
