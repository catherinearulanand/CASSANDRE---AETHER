"use client";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { StatusChip } from "@/components/ui";

/** Load helper: clears stale data on every dependency change (no old-market data shown while a new context loads). */
export function useLoad<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[], enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick(t => t + 1), []);
  useEffect(() => {
    if (!enabled) { setData(null); setError(null); setLoading(false); return; }
    const ac = new AbortController();
    setData(null); setError(null); setLoading(true);
    fn(ac.signal).then(d => { if (!ac.signal.aborted) { setData(d); setLoading(false); } })
      .catch(e => { if (!ac.signal.aborted) { setError(e as ApiError); setLoading(false); } });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, enabled]);
  // Derive "loading" synchronously: on the first render after `enabled`/deps change the effect has not run yet, so state still says
  // loading=false with data=null. Without this, consumers rendered with null data (crash found by the browser flow test).
  return { data, error, loading: loading || (enabled && data === null && error === null), reload };
}

export function stateTone(state: string): "ok" | "warn" | "danger" | "info" | "neutral" {
  if (["CHECKED", "APPROVED", "RESERVED", "DRAFTED"].includes(state)) return "ok";
  if (["PROPOSED", "HYPOTHETICAL", "NOT_REQUIRED"].includes(state)) return "info";
  if (["INFEASIBLE", "FAILED", "EXPIRED", "CANCELLED"].includes(state)) return "danger";
  return "neutral";
}
export const StateChip = ({ state }: { state: string }) => <StatusChip tone={stateTone(state)}>{state.replace(/_/g, " ")}</StatusChip>;

export const fmt = (s: string | null | undefined) => {
  if (s == null) return "—";
  const neg = s.startsWith("-"); const a = neg ? s.slice(1) : s; const [i, f] = a.split(".");
  return (neg ? "−" : "") + i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (f ? "." + f : "");
};
export const short = (h: string | null | undefined) => (h ? h.slice(0, 12) + "…" : "—");

export const REASON_TEXT: Record<string, string> = {
  NO_ENTITLEMENT: "Not entitled to this obligation (default deny)", WRONG_WAY_RISK: "Wrong-way risk (explicit issuer relationship)",
  UNKNOWN_PRICE: "Price unknown", MISSING_FX: "FX rate missing", UNKNOWN_RATING: "Rating unknown", BELOW_MIN_RATING: "Below minimum rating",
  UNKNOWN_MATURITY: "Maturity unknown", MATURITY_EXCEEDS_LIMIT: "Maturity beyond limit", SETTLEMENT_AFTER_DEADLINE: "Settles after the deadline",
  NO_AVAILABLE_UNITS: "No available units", UNKNOWN_COST: "No documented cost assumption", ZERO_COVERAGE: "Zero effective coverage",
  NO_POLICY_SOURCE: "No policy source", STRESS_UNAVAILABLE: "Made unavailable by stress", MISSING_FX_HAIRCUT: "Cross-currency without reviewed FX haircut",
  INCONSISTENT_HAIRCUT: "Inconsistent haircut/valuation in schedule", INVALID_VALUATION: "Schedule has no valuation",
};
export const reasonText = (r: string) => {
  const [code, src] = r.split(":");
  return code === "NO_SCHEDULE_MATCH" ? `Not on the ${src ?? "policy"} eligibility schedule` : (REASON_TEXT[code] ? REASON_TEXT[code] + (src ? ` (${src})` : "") : r);
};

/** Modal confirmation (approval / reservation / cancel). Focus moves in on open, Esc closes, background inert via aria-modal. */
export function ConfirmDialog({ title, open, onClose, onConfirm, pending, confirmLabel, requireReason, children }: {
  title: string; open: boolean; onClose: () => void; onConfirm: (reason: string) => void; pending: boolean; confirmLabel: string;
  requireReason?: boolean; children: ReactNode;
}) {
  const [reason, setReason] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (open) { opener.current = document.activeElement as HTMLElement | null; setReason(""); ref.current?.focus(); }
    else if (opener.current) { opener.current.focus?.(); opener.current = null; } // return focus to the control that opened the dialog
  }, [open]);
  if (!open) return null;
  // Focus trap: Tab / Shift+Tab cycle inside the modal; Escape closes (unless a request is pending).
  const trap = (e: React.KeyboardEvent) => {
    if (e.key === "Escape" && !pending) { onClose(); return; }
    if (e.key !== "Tab" || !ref.current) return;
    const f = Array.from(ref.current.querySelectorAll<HTMLElement>("button:not([disabled]), textarea, input, select, a[href], [tabindex]:not([tabindex='-1'])"));
    if (f.length === 0) { e.preventDefault(); return; }
    const first = f[0], last = f[f.length - 1], cur = document.activeElement;
    if (e.shiftKey && (cur === first || cur === ref.current)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && cur === last) { e.preventDefault(); first.focus(); }
  };
  return (
    <div role="dialog" aria-modal="true" aria-label={title} ref={ref} tabIndex={-1}
      onKeyDown={trap}
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "grid", placeItems: "center", zIndex: 50, padding: 16 }}>
      <div className="card" style={{ maxWidth: 640, width: "100%", maxHeight: "90vh", overflow: "auto" }}>
        <h2>{title}</h2>
        {children}
        {requireReason !== false && <div style={{ marginTop: 12 }}>
          <label htmlFor="cd-reason">Reason (mandatory)</label>
          <textarea id="cd-reason" value={reason} onChange={e => setReason(e.target.value)} rows={3} style={{ width: "100%", padding: 8, minHeight: 72 }} />
        </div>}
        <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
          <button onClick={onClose} disabled={pending}>Cancel</button>
          <button className="primary" disabled={pending || (requireReason !== false && !reason.trim())} onClick={() => onConfirm(reason)}>
            {pending ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: { tabs: readonly (readonly [T, string])[]; value: T; onChange: (t: T) => void; label: string }) {
  return (
    <div role="tablist" aria-label={label} className="row" style={{ borderBottom: "1px solid var(--border)", marginBottom: 16, gap: 4 }}>
      {tabs.map(([k, l], i) => (
        <button key={k} role="tab" id={`col-tab-${k}`} aria-selected={value === k} aria-controls={`col-panel-${k}`} tabIndex={value === k ? 0 : -1}
          onClick={() => onChange(k)}
          onKeyDown={e => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            const n = (i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
            onChange(tabs[n][0]); (document.getElementById(`col-tab-${tabs[n][0]}`) as HTMLButtonElement | null)?.focus();
          }}
          style={{ border: 0, borderBottom: `2px solid ${value === k ? "var(--accent)" : "transparent"}`, borderRadius: 0, background: "transparent",
            color: value === k ? "var(--accent)" : "var(--text-2)", minHeight: 44 }}>{l}</button>
      ))}
    </div>
  );
}

export const NotTransmittedBanner = () => (
  <div role="note" className="chip warn" style={{ borderRadius: 8, padding: "6px 10px", marginBottom: 12 }}>
    <span aria-hidden>!</span> Recommendation only — no collateral is posted, moved or transmitted. Reserved ≠ posted. Simulated settlement is labelled SIMULATED.
  </div>
);
