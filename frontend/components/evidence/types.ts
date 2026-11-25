export interface EvEventOut {
  sequence: number; event_id: string; engine: string; action: string; actor_type: string; entity_type: string; entity_id: string;
  entity_revision: number; correlation_id: string; causation_id: string | null; recorded_at: string; effective_at: string;
  source_mode: string; service_version: string; event_hash: string; prev_hash: string; detail_redacted: boolean;
  actor_id?: string; actor_role?: string | null; rationale?: string | null; input_hashes?: string[]; output_hash?: string | null;
  source_spans?: unknown[]; payload?: Record<string, unknown>;
}
export interface EventsPage { items: EvEventOut[]; next_cursor: number | null; detail_level: "FULL" | "METADATA_ONLY" }
export interface ChainProblem { problem: string; sequence?: number; detail?: string; anchor_sequence?: number }
export interface VerifyResult {
  valid: boolean; events: number; problems: ChainProblem[]; head_sequence: number; head_hash: string;
  anchor_status?: string; anchors_checked?: number; anchor_sequence?: number; invalid_signature_anchors?: number;
}
export interface LineageNode { node_id: string; entity_type: string; entity_id: string; stage: string; step: number; recorded: boolean; external?: boolean;
  span?: { quote?: string; excerpt?: string; page?: number | string; start?: number; end?: number; char_start?: number; char_end?: number };
  events: { sequence: number; action: string; engine: string; recorded_at: string; event_hash: string; correlation_id?: string }[] }
export interface Lineage {
  run_id: string | null; correlation_id: string; nodes: LineageNode[]; edges: { from: string; to: string }[]; required_stages: string[];
  missing: { kind: string; stage?: string; node_id?: string; detail: string }[]; complete: boolean; event_count: number;
}
export interface Integrity {
  status: "OK" | "DEGRADED" | "FAILED"; problems: string[]; chain: VerifyResult; objects: { objects: number; valid: boolean; problems: unknown[] };
  storage: { writable: boolean; path: string }; head_sequence: number; events_since_last_checkpoint: number;
  last_checkpoint: { sequence: number; created_at: string; key_id: string } | null;
  outbox: { pending: number; dead: number; delivered: number }; signing: { current_key_id: string; protection: string };
}
export interface ExportOut { export_id: string; run_id: string; redacted: boolean; events: number; bundle_hash: string; created_at?: string;
  checkpoint_sequence?: number; missing_objects?: { content_hash: string; reason: string }[]; lineage_complete?: boolean }
export interface ExportDetail extends ExportOut {
  preview: { files: Record<string, { sha256: string; size: number }>; missing_objects: { content_hash: string; reason: string }[];
    chain: { checkpoint_sequence: number; checkpoint_head_hash: string; key_id: string }; redaction_note: string | null; scope: string; limitations: string[] };
}
export interface ReplayOut { replay_id: string; run_id: string; status: string; read_only: boolean; caveats: string[];
  items: { sequence: number; action: string; engine: string; status: string; reason?: string }[]; first_divergence: unknown }
export interface TamperOut { export_id: string; mode: string; verified: boolean; problems: string[]; operational_evidence_modified: boolean; note: string }
export interface PublicKey { key_id: string; current: boolean; algorithm: string; public_key_pem: string; protection: string }
export interface EntityResolution {
  entity_id: string; found: boolean; message?: string; lineage: Lineage | null;
  decisions: { correlation_id: string; run_id: string | null; match: "RUN" | "ENTITY" | "PARENT" }[];
}
