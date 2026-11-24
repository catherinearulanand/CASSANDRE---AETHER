// Types mirror backend/app/engines/collateral API payloads. All amounts are decimal STRINGS (never JS numbers).
export interface Snapshot { snapshot_id: string; as_of: string; currency: string; market_type: string; content_hash: string; lot_count: number; has_policy: boolean; wrong_way_issuer_groups: string[] }
export interface Leg {
  lot_id: string; asset_class: string; currency: string; quantity: string; price: string; fx_rate: string; gross_value: string;
  valuation_factor: string; fx_factor: string; effective_coverage: string; cost_horizon: string; share_of_coverage: string; settlement_lag_days: number;
}
export interface Constraint { name: string; kind: string; limit: string; measured: string; slack: string; binding: boolean; unit: string }
export interface Check { name: string; ok: boolean; detail: string }
export interface Baseline {
  status: string; policy?: string | null; cost?: string; savings_absolute?: string | null; savings_pct?: string | null; units?: string;
  comparability?: string; legs?: Leg[]; tried?: { policy: string; feasible: boolean; reason?: string }[];
}
export interface ProposalResult {
  solver_status: string; solver?: Record<string, unknown>; solver_message?: string; runtime_s?: number; integer_lots?: boolean;
  exclusions?: { lot_id: string; reasons: string[] }[]; warnings?: string[]; coverage?: string; repair_applied?: boolean;
  coverage_shadow_price?: { value: string; unit: string; note: string } | null;
  rounding?: { lp_objective: string; rounded_cost: string; absolute_gap: string; note: string };
  diagnostics?: { achievable_coverage?: string | null; remaining?: string | null; binding_constraints?: string[]; lots_at_availability_limit?: string[]; note?: string };
  verification?: { ok: boolean; checks: Check[]; failures: Check[]; coverage: string; required: string } | null;
  mip_gap?: number | null; expiry_reason?: string;
}
export interface Proposal {
  proposal_id: string; kind: string; state: string; solver_status: string; hypothetical: boolean; market_type: string; obligation_id: string;
  currency: string; required_coverage: string; objective: string | null; residual: string | null; revision: number; proposal_hash: string;
  margin_result_id: string; inventory_snapshot_id: string; created_at: string; expires_at: string; approval_id: string | null;
  created_by: string; horizon_days: number; deadline: string; transmission: string; correlation_id?: string; maker_id?: string;
  simulated_settlement: { status: string; label: string; at: string } | null;
  margin_result_hash?: string | null; inventory_hash?: string; terms_hash?: string | null; rule_set_hash?: string | null; as_of?: string;
  baseline?: Baseline; result?: ProposalResult; legs?: Leg[]; constraints?: Constraint[]; cost_units?: string;
  approval?: { approval_id: string; decision: string; target_hash: string; expires_at: string };
}
export interface Desk {
  currency: string; market_type: string; obligation_id: string; required_coverage_still_needed: string;
  posted_coverage: { eligible_posted: { amount: string; currency: string }; snapshot_id: string; excluded: { lot_id: string; reasons: string[] }[] } | null;
  eligible_inventory: { asset_class: string; currency: string; lots: number; available_units: string; effective_coverage: string }[];
  eligible_effective_coverage_by_deadline: string; achievable_under_caps: string | null; shortfall_vs_eligible: string;
  currency_shortfall: Record<string, string>; cash_buffer_effective: string;
  late_settling_lots: { lot_id: string; settlement_lag_days: number }[];
  exclusions_by_reason: Record<string, number>; excluded_lots: { lot_id: string; reasons: string[] }[];
  active_reservations: { count: number; units: string }; warnings: string[]; snapshot: Snapshot; deadline: string; horizon_days: number;
}
export interface StressOut {
  hypothetical: boolean; label: string; stress: Record<string, unknown>; objective_delta: string | null;
  base: { solver_status: string; objective: string | null; residual: string | null; coverage?: string; eligible_lots?: number } | null; stressed: Proposal;
}
export interface Instruction { proposal_id: string; status: string; content_hash: string; created_at: string; content: Record<string, unknown> & { watermark: string } }
export interface Ctx { margin: string; snap: string; deadline: string; horizon: number }
