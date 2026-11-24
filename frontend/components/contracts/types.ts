export interface Money { amount: string; currency: string }
export interface FieldCounts { PROPOSED: number; UNRESOLVED: number; APPROVED: number; REJECTED: number }
export interface PkgSummary {
  package_id: string; master_id: string; csa_id: string; netting_set_id: string; party_a_id: string; party_b_id: string;
  party_a_name: string; party_b_name: string; base_currency: string; governing_law: string; status: string; revision: number;
  documents: number; docs_with_missing_annexes: number; docs_pending_extraction: number; fields: FieldCounts;
  outstanding_review: number; latest_version_no: number | null; trades_in_netting_set: number; provenance: string;
}
export interface DocMeta {
  doc_id: string; kind: string; title: string; doc_ref: string; sha256: string; effective_from: string; rank: number;
  missing_annexes: string[]; extracted_mode: string | null; chars: number;
}
export interface VersionMeta {
  version_id: string; version_no: number; bundle_hash: string; status: string; frozen_at: string; maker_id: string;
  checker_id: string; approval_id: string; supersedes_version_id: string | null; required_facts: string[];
}
export interface PkgDetail extends PkgSummary { document_list: DocMeta[]; versions: VersionMeta[] }
export type FieldValue = Money | string | boolean | Record<string, unknown>[] | null;
export interface Field {
  field_id: string; doc_id: string; doc_ref: string | null; field_key: string; direction: "A" | "B" | null; value: FieldValue;
  original_value: FieldValue; quote: string; start_char: number; end_char: number; condition: Record<string, unknown> | null;
  effective_from: string; rank: number; extraction_mode: string; status: "PROPOSED" | "UNRESOLVED" | "APPROVED" | "REJECTED";
  issues: string[]; maker_id: string; reviewer_id: string | null; review_reason: string | null; revision: number; frozen: boolean; material: boolean;
}
export interface EligRow {
  field_id: string; asset_class: string; currency: string; issuer_class: string; valuation_pct: string; haircut_pct: string;
  fx_haircut_pct: string; concentration_cap_pct: string | null; cap_denominator: string | null; min_rating: string | null;
  max_maturity_years: string | null; field_status: string;
}
export interface Readiness { ready_to_freeze: boolean; blocking_reasons: string[]; candidate_bundle_hash: string; required_fields: string[]; unchanged_from_latest_frozen: boolean }
export interface TermsView { package: PkgSummary; fields: Field[]; readiness: Readiness; eligibility: EligRow[] }
export interface Approval { approval_id: string; decision: string; target_hash: string; target_revision: number; maker_id: string; expires_at: string }
export interface Membership { trade_id: string; netting_set_id: string | null; master_id: string | null; status: string; reason: string; declared_master_id: string | null; declared_netting_set_id: string | null }
export interface Resolution {
  status: "RESOLVED" | "BLOCKED"; terms_hash: string | null; terms: Record<string, unknown> | null; blocking_reasons: string[];
  required_facts: string[]; version_no?: number; as_of: string; regulatory_constraints: string;
}
export const isMoney = (v: unknown): v is Money => !!v && typeof v === "object" && "amount" in (v as object) && "currency" in (v as object);
