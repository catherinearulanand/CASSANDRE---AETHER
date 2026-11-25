"use client";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { ErrorState, StatusChip } from "@/components/ui";

export interface Rule {
  rule_id: string; rule_key: string; version: number; status: string; effective_state: string; authority_type: string; title: string;
  markets: string[]; effective_from: string; effective_to: string | null; recorded_at: string; activated_at: string | null;
  withdrawn_at: string | null; supersedes_rule_id: string | null; superseded_by_rule_id: string | null; content_hash: string;
  approved_hash: string | null; maker_id: string; revision: number; compile_ok: boolean; conflict_group: string | null;
  precedence_rank: number | null; provenance: string; span_ids: string[];
  scope?: unknown; condition?: unknown; parameters?: Record<string, { value: string; unit: string }>; rationale?: string;
  compile_report?: { ok: boolean; errors: { path: string; message: string }[]; dependencies: string[] };
  review_notes?: { actor: string; decision: string; reason: string; at: string }[];
  tests?: { test_id: string; vector: { name?: string; context: Record<string, unknown>; expect_scope?: string; expect_condition?: string }; passed: boolean | null; last_result: Record<string, unknown> }[];
  spans?: Span[];
  approvals?: { approval_id: string; decision: string; maker_id: string; checker_id: string | null; target_hash: string; target_revision: number; expires_at: string; decided_at: string | null; reason: string }[];
}
export interface Span { span_id: string; source_id: string; page: number; start: number; end: number; byte_start: number | null; byte_end: number | null; quote: string; method: string }
export interface Source {
  source_id: string; authority_type: string; doc_kind: string; title: string; issuer: string | null; doc_number: string | null; kind: string;
  sha256: string; page_count: number; unreadable_pages: number[]; risk_flags: string[]; classification: string; verification: string;
  retrieved_at: string; provenance: string; text?: string; official_uri: string | null; deduplicated?: boolean;
}
export interface Candidate {
  candidate_id: string; source_id: string; mode: string; fields: Record<string, unknown>; span_ids: string[]; issues: string[];
  grounded: boolean; confidence_category: string; status: string;
}

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [n, setN] = useState(0);
  const fnRef = useRef(fn); fnRef.current = fn;
  useEffect(() => {
    let live = true;
    setData(null); setErr(null); setLoading(true); // clear stale data whenever the context/selection changes
    fnRef.current().then(d => { if (live) setData(d); }).catch(e => { if (live) setErr(e as ApiError); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  const reload = useCallback(() => setN(x => x + 1), []);
  return { data, err, loading, reload };
}

const TONE: Record<string, "ok" | "warn" | "danger" | "info" | "neutral"> = {
  ACTIVE: "ok", SCHEDULED: "info", APPROVED: "info", REVIEW_REQUIRED: "warn", DRAFT: "neutral", SUPERSEDED: "neutral", WITHDRAWN: "danger",
};
export const RuleStatus = ({ s }: { s: string }) => <StatusChip tone={TONE[s] ?? "neutral"}>{s.replace(/_/g, " ")}</StatusChip>;
export const short = (h: string | null | undefined) => (h ? h.slice(0, 12) + "…" : "—");
export const fmt = (iso: string | null | undefined) => (iso ? iso.replace("T", " ").replace(/\.\d+/, "").replace("+00:00", "Z") : "—");
export const newKey = () => crypto.randomUUID();

export function Json({ v }: { v: unknown }) {
  return <pre className="mono" style={{ margin: 0, padding: 8, background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6, overflowX: "auto", fontSize: 12 }}>{JSON.stringify(v, null, 2)}</pre>;
}

/** Approval/confirm dialog: mandatory reason, before/after facts, hashes, actor; disabled while pending; no fake success. */
const FOCUSABLE = "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function ReasonDialog({ title, facts, confirmLabel, onConfirm, onClose, tone = "primary" }: {
  title: string; facts: [string, ReactNode][]; confirmLabel: string; tone?: "primary" | "danger";
  onConfirm: (reason: string) => Promise<void>; onClose: () => void;
}) {
  const [reason, setReason] = useState(""); const [busy, setBusy] = useState(false); const [err, setErr] = useState<ApiError | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null); const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Modal behaviour: remember the trigger, move focus in, lock background scroll, and on close give focus back
    // (to the trigger if it still exists and is enabled, otherwise to the page heading so keyboard users keep their place).
    const opener = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const prev = document.body.style.overflow; document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
      if (opener && opener.isConnected && !(opener as HTMLButtonElement).disabled) opener.focus();
      else { const h = document.querySelector<HTMLElement>("main h1"); if (h) { h.setAttribute("tabindex", "-1"); h.focus(); } }
    };
  }, []);
  async function go() {
    setBusy(true); setErr(null);
    try { await onConfirm(reason.trim()); onClose(); } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }
  function key(e: React.KeyboardEvent) {
    if (e.key === "Escape" && !busy) { e.stopPropagation(); onClose(); return; }
    if (e.key !== "Tab" || !box.current) return;
    const f = Array.from(box.current.querySelectorAll<HTMLElement>(FOCUSABLE)); if (f.length === 0) return;
    const first = f[0], last = f[f.length - 1], cur = document.activeElement;
    if (e.shiftKey && (cur === first || !box.current.contains(cur))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (cur === last || !box.current.contains(cur))) { e.preventDefault(); first.focus(); }
  }
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="dlg-title" aria-describedby="dlg-facts" onKeyDown={key}
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", display: "grid", placeItems: "center", zIndex: 50, padding: 12 }}>
      <div className="card" ref={box} style={{ width: "min(560px, 100%)", maxHeight: "90vh", overflow: "auto" }}>
        <h2 id="dlg-title">{title}</h2>
        <table id="dlg-facts"><caption className="sr-only">Details of the action you are about to record</caption><tbody>{facts.map(([k, v]) => <tr key={k}><th scope="row" style={{ width: 150 }}>{k}</th><td className="mono" style={{ wordBreak: "break-all" }}>{v}</td></tr>)}</tbody></table>
        <label htmlFor="reason" style={{ marginTop: 12 }}>Reason (mandatory, recorded as evidence)</label>
        <textarea id="reason" ref={ref} value={reason} onChange={e => setReason(e.target.value)} rows={3} style={{ width: "100%", padding: 8 }} aria-required="true" />
        {err && <div style={{ marginTop: 8 }} role="alert"><ErrorState error={err} />{Object.entries(err.body.field_errors ?? {}).map(([k, v]) => <div className="field-error" key={k}>{k}: {v}</div>)}</div>}
        <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <button onClick={onClose} disabled={busy}>Cancel</button>
          <button className={tone === "danger" ? "" : "primary"} disabled={busy || !reason.trim()} onClick={go}>{busy ? "Working…" : confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: readonly (readonly [T, string])[]; value: T; onChange: (t: T) => void; label: string }) {
  return (
    <div role="tablist" aria-label={label} className="row" style={{ borderBottom: "1px solid var(--border)", marginBottom: 16, gap: 4 }}>
      {tabs.map(([k, l], i) => (
        <button key={k} id={`rtab-${k}`} role="tab" aria-selected={value === k} aria-controls={`rpanel-${k}`} tabIndex={value === k ? 0 : -1} onClick={() => onChange(k)}
          onKeyDown={e => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            const n = (i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length; onChange(tabs[n][0]);
            setTimeout(() => document.getElementById(`rtab-${tabs[n][0]}`)?.focus(), 0);
          }}
          style={{ border: 0, borderBottom: `2px solid ${value === k ? "var(--accent)" : "transparent"}`, borderRadius: 0, background: "transparent", color: value === k ? "var(--accent)" : "var(--text-2)", minHeight: 44 }}>{l}</button>
      ))}
    </div>
  );
}
