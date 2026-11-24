export interface Amt { amount: string; currency: string }
export interface WaterfallStep { step: string; value: string; citation: string }
export interface Component { kind: string; amount: string; currency: string; [k: string]: unknown }
export interface TradeVal {
  trade_id: string; type: string; side: string; netting_set_id: string | null; master_id: string;
  pv_trade_ccy: Amt; pv_base: Amt; fx_used: string | null; maturity: string;
}
export interface MarginResult {
  result_id: string; market_type: string; status: "OK" | "BLOCKED" | "UNSUPPORTED"; blocking_reasons: string[];
  currency: string | null; account_id: string | null; netting_set_id: string | null; as_of: string;
  exposure: Amt | null; required_amount: Amt | null; direction: "POST" | "RETURN" | "NONE";
  unrounded_amount: Amt | null; rounded_amount: Amt | null; components: Component[]; waterfall: WaterfallStep[];
  input_hashes: string[]; snapshot_id: string | null; terms_hash: string | null; rule_set_hash: string | null;
  method: string; method_version: string; hypothetical: boolean; provenance_flags: string[]; warnings: string[];
  quality: Record<string, unknown>; trade_valuations: TradeVal[];
  netting: { gross_positive: Amt; gross_negative: Amt; net: Amt; netting_benefit: Amt; netting_benefit_definition: string } | null;
  baseline_amount?: Amt; internal_estimate: null | { label: string; estimated_loss: string; shock_pct: string; metrics: Record<string, string>; by_instrument: Record<string, string>[] };
  result_hash: string;
}
export interface Snapshot { snapshot_id: string; data_as_of: string; content_hash: string; max_age_seconds: number; curves: string[]; fx_pairs: string[] }
export interface ClearingFile {
  file_id: string; revision: number; account_id: string; venue: string; currency: string; as_of: string; status: string;
  computed_total: Amt; reported_total: Amt | null; reconciliation: { reason: string | null; difference: string | null };
  rows: { component: string; amount: string; currency: string; kind: string }[]; content_hash: string;
}
export interface ResultRow { result_id: string; market_type: string; subject_id: string | null; status: string; created_at: string; required_amount: Amt | null; direction: string; hypothetical: boolean }
export interface StressOut {
  stress_id: string; base_result_id: string; label: string; baseline_mutated: boolean;
  baseline: Record<string, unknown>; scenario: Record<string, unknown>; delta: Record<string, Amt>;
  dv01?: { value: Amt; method: string }; breakdown: Record<string, unknown>[];
}
export interface Methodology { method_version: string; numerics: string; methods: Record<string, { summary: string; formulas: string[]; limitations: string[] }>; conventions: Record<string, string> }
export const METHOD_FOR: Record<string, string> = { OTC: "OTC_VM_SIMPLE_V1", ETD_DOMESTIC: "ETD_IMPORTED_V1", ETD_IFSC: "ETD_IMPORTED_V1" };
export const ETD_ACCOUNT: Record<string, { account: string; venue: string }> = {
  ETD_DOMESTIC: { account: "ACC-DEMO-NSE-001", venue: "DEMO_NSE" }, ETD_IFSC: { account: "ACC-DEMO-IFSC-001", venue: "DEMO_IFSC" },
};
export const DIRECTION_TEXT: Record<string, string> = {
  POST: "POST — obligor posts collateral (call)", RETURN: "RETURN — excess collateral is returned", NONE: "NONE — no transfer required",
};
