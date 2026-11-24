"use client";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { StatusChip } from "@/components/ui";

export type Tone = "ok" | "warn" | "danger" | "info" | "neutral";
export const API = "/compliance";

export interface Evaluation {
  evaluation_id: string; control_key: string; rule_key: string; rule_version: number; rule_hash: string; subject_id: string;
  market_type: string; venue: string | null; status: string; severity: string; reasons: string[]; trace: { scope: unknown[]; condition: Record<string, unknown>[]; rule_currency?: string };
  measured: string | null; limit: string | null; gap: string | null; unit: string | null; as_of: string; snapshot_id: string | null;
  margin_result_id: string | null; allocation_id: string | null; facts: Record<string, unknown>; fact_provenance: Record<string, string>;
  waiver_id: string | null; case_id: string | null; is_recheck: boolean; created_at: string;
}
export interface CaseRow {
  case_id: string; kind: string; control_key: string; rule_version: number; family: string; subject_id: string; market_type: string; title: string;
  state: string; severity: string; last_status: string; owner_id: string | null; maker_id: string | null; opened_as_of: string; last_as_of: string; due_at: string;
  occurrence_count: number; reopen_count: number; margin_result_id: string | null; allocation_id: string | null; remediations: Remediation[];
  pending_dismissal: { requested_by: string; reason: string } | null; last_recheck: { evaluation_id: string; status: string; as_of: string } | null;
  waiver_id: string | null; resolution: Record<string, string> | null; revision: number;
}
export interface Remediation { id: string; selected: RemedyOption; rejected: { route: string; reason: string }[]; rationale: string; deadline: string; owner_id: string; proposed_by: string }
export interface RemedyOption { route: string; label: string; target_engine: string | null; consequence: string; inputs?: Record<string, string | null> }
export interface CaseDetail extends CaseRow {
  events: { id: number; event_type: string; actor_id: string; from_state: string | null; to_state: string | null; comment: string | null; as_of: string | null; recorded_at: string }[];
  evaluations: Evaluation[]; remedy_options: RemedyOption[]; separation_note: string; anomaly_disclaimer: string | null;
  control: { control_key: string; title: string; rule_key: string; rule_version: number; rule_hash: string; authority_type: string; scope: unknown; condition: unknown } | null;
}

export function statusTone(s: string): Tone {
  return s === "PASS" ? "ok" : s === "FAIL" || s === "CONFLICT" || s === "ERROR" ? "danger" : s === "UNKNOWN" ? "warn" : "neutral";
}
export function Status({ s }: { s: string }) { return <StatusChip tone={statusTone(s)}>{s.replace("_", " ")}</StatusChip>; }
export function Severity({ s }: { s: string }) { return <StatusChip tone={s === "CRITICAL" || s === "HIGH" ? "danger" : s === "MEDIUM" ? "warn" : "neutral"}>{s}</StatusChip>; }

/** Load with abort + explicit stale handling: data is cleared when the key changes, so old-context data is never shown. */
export function useLoad<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const silent = useRef(false);
  // reload() keeps the current data on screen (so inline errors survive); a changed path/deps clears it (no stale-context data).
  const reload = useCallback(() => { silent.current = true; setTick(t => t + 1); }, []);
  useEffect(() => {
    if (!path) return;
    const ac = new AbortController();
    const quiet = silent.current; silent.current = false;
    if (!quiet) { setLoading(true); setData(null); }
    setError(null);
    api<T>(path, { signal: ac.signal }).then(d => { setData(d); setLoading(false); })
      .catch(e => { if (!ac.signal.aborted) { setError(e as ApiError); setLoading(false); } });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps]);
  return { data, error, loading, reload };
}

/** POST with a fresh idempotency key per user action; surfaces field errors; disables while pending. */
export function useAction<T = unknown>() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<T | null>(null);
  const run = useCallback(async (path: string, body: unknown): Promise<T | null> => {
    setPending(true); setError(null);
    try { const r = await api<T>(path, { method: "POST", body, idempotencyKey: newKey() }); setResult(r); return r; }
    catch (e) { setError(e as ApiError); return null; }
    finally { setPending(false); }
  }, []);
  return { run, pending, error, result, clear: () => { setError(null); setResult(null); } };
}

export function FieldErrors({ error }: { error: ApiError | null }) {
  if (!error) return null;
  const fe = Object.entries(error.body.field_errors ?? {});
  return (
    <div className="field-error" role="alert">
      <strong>{error.body.code}</strong>: {error.message}
      {fe.length > 0 && <ul style={{ margin: "4px 0 0 16px" }}>{fe.map(([k, v]) => <li key={k}>{k}: {v}</li>)}</ul>}
      <div className="mono faint">correlation_id {error.body.correlation_id}</div>
    </div>
  );
}

/** Confirmation dialog for consequential actions (never one-click). Focus moves into the dialog; Escape cancels. */
export function ConfirmAction({ label, title, children, onConfirm, disabled, primary }: {
  label: string; title: string; children: ReactNode; onConfirm: () => void; disabled?: boolean; primary?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const d = ref.current; if (!d) return; if (open && !d.open) d.showModal(); if (!open && d.open) d.close(); }, [open]);
  return (
    <>
      <button className={primary ? "primary" : undefined} disabled={disabled} onClick={() => setOpen(true)}>{label}</button>
      <dialog ref={ref} onClose={() => setOpen(false)} onCancel={() => setOpen(false)} aria-labelledby="cf-title"
        style={{ background: "var(--panel)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 10, maxWidth: 520, width: "92vw" }}>
        <h2 id="cf-title">{title}</h2>
        <div className="muted">{children}</div>
        <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
          <button onClick={() => setOpen(false)}>Cancel</button>
          <button className="primary" onClick={() => { setOpen(false); onConfirm(); }}>Confirm</button>
        </div>
      </dialog>
    </>
  );
}

export function Amount({ value, unit }: { value: string | null; unit: string | null }) {
  if (value == null) return <span className="faint">—</span>;
  if (unit === "INR" || unit === "USD") {
    const [i, f] = value.split("."); const g = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return <span className="num" title={`${value} ${unit}`}>{g}{f ? "." + f : ""} {unit}</span>;
  }
  return <span className="num">{value}{unit ? ` ${unit}` : ""}</span>;
}

export const fmtTs = (s: string | null | undefined) => (s ? s.replace("T", " ").replace("+00:00", "Z").replace(/\.\d+/, "") : "—");

export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: readonly (readonly [T, string])[]; value: T; onChange: (t: T) => void; label: string }) {
  return (
    <div role="tablist" aria-label={label} className="row" style={{ borderBottom: "1px solid var(--border)", marginBottom: 16, gap: 4 }}>
      {tabs.map(([k, l], i) => (
        <button key={k} id={`cmp-tab-${k}`} role="tab" aria-selected={value === k} aria-controls={`cmp-panel-${k}`} tabIndex={value === k ? 0 : -1}
          onClick={() => onChange(k)}
          onKeyDown={e => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") { const n = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length][0]; onChange(n); document.getElementById(`cmp-tab-${n}`)?.focus(); } }}
          style={{ border: 0, borderBottom: `2px solid ${value === k ? "var(--accent)" : "transparent"}`, borderRadius: 0, background: "transparent",
            color: value === k ? "var(--accent)" : "var(--text-2)", minHeight: 44 }}>{l}</button>))}
    </div>
  );
}
