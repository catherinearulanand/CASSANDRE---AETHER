"use client";
import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";
import { api, ApiError } from "@/lib/api";
import { PINNED_AS_OF } from "@/lib/session";
import { EmptyState, ErrorState, MoneyDisplay, StatusChip } from "@/components/ui";
import type { ClearingFile, PositionMetrics, ReconStatus } from "./types";

export const VENUE: Record<string, string> = { ETD_DOMESTIC: "DEMO_NSE", ETD_IFSC: "DEMO_IFSC" };
export const CCY: Record<string, string> = { ETD_DOMESTIC: "INR", ETD_IFSC: "USD" };

function FieldErrors({ e }: { e: ApiError }) {
  const fe = Object.entries(e.body.field_errors ?? {});
  return (
    <div className="state error" role="alert" style={{ textAlign: "left", marginTop: 12 }}>
      <strong>{e.body.code}</strong>: {e.message}
      {fe.length > 0 && <ul aria-label="Field errors">{fe.map(([k, v]) => <li key={k}><span className="mono">{k}</span>: {v}</li>)}</ul>}
      <div className="mono faint">correlation_id {e.body.correlation_id}</div>
    </div>
  );
}

/** Atomic import of a clearing-style margin file: checksum/duplicate/total reconciliation happens server-side; nothing is committed on error. */
export function ClearingImport({ market, canWrite, onDone }: { market: string; canWrite: boolean; onDone: () => void }) {
  const venue = VENUE[market];
  const [csv, setCsv] = useState(""); const [rev, setRev] = useState("1"); const [declared, setDeclared] = useState("");
  const [out, setOut] = useState<ClearingFile | null>(null); const [err, setErr] = useState<ApiError | null>(null); const [busy, setBusy] = useState(false);
  useEffect(() => { setOut(null); setErr(null); setCsv(""); }, [market]); // never show another market's import result

  function onFile(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) f.text().then(setCsv);
  }
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null); setOut(null);
    try {
      const r = await api<ClearingFile>("/risk/clearing/import", { method: "POST",
        body: { csv_text: csv, venue, revision: Math.max(1, parseInt(rev, 10) || 1), ...(declared ? { declared_total: declared } : {}) } });
      setOut(r); onDone();
    } catch (x) { setErr(x as ApiError); } finally { setBusy(false); }
  }
  return (
    <form className="card" onSubmit={submit} aria-labelledby="imp-h" style={{ marginTop: 16 }}>
      <h2 id="imp-h">Import clearing-style margin file ({venue} · {CCY[market]})</h2>
      <p className="muted" style={{ marginTop: 0 }}>Synthetic CSV only: file_id, account_id, component, amount, currency, aggregation_kind, as_of. The import is atomic — a bad row, duplicate or checksum mismatch rejects the whole file.</p>
      <div className="grid c3">
        <div><label htmlFor="csvfile">CSV file</label><input id="csvfile" type="file" accept=".csv,text/csv" onChange={onFile} /></div>
        <div><label htmlFor="rev">Revision</label><input id="rev" value={rev} onChange={e => setRev(e.target.value)} inputMode="numeric" /></div>
        <div><label htmlFor="decl">Declared total (optional, {CCY[market]})</label><input id="decl" value={declared} onChange={e => setDeclared(e.target.value)} inputMode="decimal" placeholder="reconciled against the component sum" /></div>
      </div>
      <div style={{ marginTop: 12 }}><label htmlFor="csvtext">…or paste CSV text</label>
        <textarea id="csvtext" value={csv} onChange={e => setCsv(e.target.value)} rows={5} spellCheck={false} className="mono" style={{ width: "100%", padding: 8, minHeight: 100 }} /></div>
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy || !canWrite || !csv.trim()} title={canWrite ? "" : "Requires maker role"}>{busy ? "Importing…" : "Import file"}</button>
        <span className="faint">{canWrite ? "Imported margin is a source obligation, not a computed SPAN figure." : "Read-only role: import disabled."}</span>
      </div>
      {err && <FieldErrors e={err} />}
      {out && (
        <div className="state" role="status" style={{ textAlign: "left", marginTop: 12 }}>
          <div className="row"><strong className="mono">{out.file_id} · r{out.revision}</strong>
            {out.status === "RECONCILED" ? <StatusChip tone="ok">RECONCILED</StatusChip> : <StatusChip tone="danger">EXCEPTION</StatusChip>}</div>
          <p style={{ margin: "8px 0 0" }}>Independent sum of components: <MoneyDisplay {...out.computed_total} signed={false} /> · reported total: {out.reported_total ? <MoneyDisplay {...out.reported_total} signed={false} /> : "none supplied"}
            {out.reconciliation.difference != null && <> · difference <span className="num">{out.reconciliation.difference} {out.currency}</span></>}</p>
          {out.status !== "RECONCILED" && <p className="muted" style={{ margin: "4px 0 0" }}>{out.reconciliation.reason} — margin calculations that use this file are BLOCKED until a corrected revision is imported.</p>}
        </div>)}
    </form>
  );
}

/** Raw metrics for position-limit rules (contract v1.1). Unknown values are shown as UNKNOWN, never as zero. */
export function PositionMetricsCard({ market, account }: { market: string; account: string }) {
  const [m, setM] = useState<PositionMetrics | null>(null); const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    const ac = new AbortController(); setM(null); setErr(null);
    api<PositionMetrics>(`/risk/position-metrics?account_id=${encodeURIComponent(account)}&as_of=${encodeURIComponent(PINNED_AS_OF)}`, { signal: ac.signal })
      .then(setM).catch(e => { if (!ac.signal.aborted) setErr(e); });
    return () => ac.abort();
  }, [market, account]);
  const v = (x: string | null, unit: string) => (x == null ? <span className="faint">UNKNOWN (not zero)</span> : <span className="num">{x} {unit}</span>);
  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="pm-h"><h2 id="pm-h">Position metrics for limit rules — {account}</h2>
      {err ? <ErrorState error={err} /> : !m ? <EmptyState title="Loading position metrics…" /> : !m.available ? <EmptyState title="No positions imported for this account">{m.warnings.join(" ")}</EmptyState> : (
        <>
          <div className="scroll-x"><table><caption className="sr-only">Position metrics</caption><thead><tr><th>Metric</th><th className="r">Value</th></tr></thead><tbody>
            <tr><td>Gross lots (Σ|lots|)</td><td className="r">{v(m.gross_lots, "lots")}</td></tr>
            <tr><td>Net lots (Σ signed lots)</td><td className="r">{v(m.net_lots, "lots")}</td></tr>
            <tr><td>Delta-equivalent lots</td><td className="r">{v(m.delta_equivalent_lots, "lots")}</td></tr>
            <tr><td>Notional (Σ|lots|×multiplier×mark)</td><td className="r">{v(m.notional, m.currency ?? "")}</td></tr>
            <tr><td>Client classification</td><td className="r"><StatusChip tone="warn">{m.client_class}</StatusChip></td></tr>
          </tbody></table></div>
          <p className="mono faint">positions as of {m.as_of} · {m.source}</p>
          <p className="faint">{m.client_class_note}</p>
          {m.warnings.length > 0 && <ul className="muted">{m.warnings.map((w, i) => <li key={i}>⚠ {w}</li>)}</ul>}
        </>)}
    </section>
  );
}

/** Latest clearing file status for the account at the pinned as-of, recomputed from stored rows. UNKNOWN is never shown as OK. */
export function ReconStatusCard({ market, account, refreshKey }: { market: string; account: string; refreshKey: number }) {
  const [r, setR] = useState<ReconStatus | null>(null); const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    const ac = new AbortController(); setR(null); setErr(null);
    api<ReconStatus>(`/risk/reconciliation-status?account_id=${encodeURIComponent(account)}&as_of=${encodeURIComponent(PINNED_AS_OF)}`, { signal: ac.signal })
      .then(setR).catch(e => { if (!ac.signal.aborted) setErr(e); });
    return () => ac.abort();
  }, [market, account, refreshKey]);
  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="rs-h"><h2 id="rs-h">Reconciliation status — {account}</h2>
      {err ? <ErrorState error={err} /> : !r ? <EmptyState title="Loading…" /> : (
        <div className="row">
          {r.status === "OK" ? <StatusChip tone="ok">OK</StatusChip> : r.status === "EXCEPTION" ? <StatusChip tone="danger">EXCEPTION</StatusChip> : <StatusChip tone="warn">UNKNOWN</StatusChip>}
          {r.file_id ? <span className="mono">{r.file_id} · r{r.revision}</span> : <span className="muted">{r.reason}</span>}
          {r.file_id && <span>expected (component sum) <span className="num">{r.expected_total} {r.currency}</span> · imported total <span className="num">{r.imported_total ?? "none"} {r.imported_total ? r.currency : ""}</span>{r.difference && r.difference !== "0" && <> · difference <span className="num">{r.difference}</span></>}</span>}
        </div>)}
    </section>
  );
}
