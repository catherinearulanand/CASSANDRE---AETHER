"use client";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import type { PkgSummary } from "./types";

const STATUS_TONE: Record<string, "ok" | "warn" | "info" | "neutral"> = { FROZEN: "ok", IN_REVIEW: "warn", AMENDMENT_IN_REVIEW: "warn", DRAFT: "neutral" };

export default function Registry({ selected, onSelect, refreshKey }: { selected: string | null; onSelect: (id: string) => void; refreshKey: number }) {
  const [items, setItems] = useState<PkgSummary[] | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [status, setStatus] = useState(""); const [currency, setCurrency] = useState(""); const [review, setReview] = useState("");

  useEffect(() => {
    const ac = new AbortController();
    const q = new URLSearchParams();
    if (status) q.set("status", status); if (currency) q.set("currency", currency); if (review) q.set("needs_review", review);
    setItems(null); setErr(null);
    api<{ items: PkgSummary[] }>(`/contracts/packages?${q}`, { signal: ac.signal }).then(r => setItems(r.items)).catch(e => { if (!ac.signal.aborted) setErr(e); });
    return () => ac.abort();
  }, [status, currency, review, refreshKey]);

  return (
    <section className="card" aria-labelledby="reg-h">
      <h2 id="reg-h">Agreement registry</h2>
      <div className="row" role="search" aria-label="Registry filters" style={{ marginBottom: 12 }}>
        <div><label htmlFor="f-status">Status</label>
          <select id="f-status" value={status} onChange={e => setStatus(e.target.value)}>
            <option value="">All</option><option>DRAFT</option><option>IN_REVIEW</option><option>FROZEN</option><option>AMENDMENT_IN_REVIEW</option></select></div>
        <div><label htmlFor="f-ccy">Base currency</label>
          <select id="f-ccy" value={currency} onChange={e => setCurrency(e.target.value)}><option value="">All</option><option>USD</option><option>INR</option><option>EUR</option></select></div>
        <div><label htmlFor="f-rev">Outstanding review</label>
          <select id="f-rev" value={review} onChange={e => setReview(e.target.value)}><option value="">Any</option><option value="true">Has outstanding</option><option value="false">None</option></select></div>
      </div>
      {err ? <ErrorState error={err} /> : !items ? <EmptyState title="Loading agreements…" /> : items.length === 0
        ? <EmptyState title="No agreements match">Create a package via the API (POST /contracts/packages) or seed synthetic fixtures.</EmptyState>
        : <div className="scroll-x" role="region" tabIndex={0} aria-label="Agreement registry table (scrollable)"><table>
          <caption className="sr-only">Bilateral agreement packages</caption>
          <thead><tr><th>Netting set</th><th>Master / CSA</th><th>Party A vs Party B</th><th>Ccy</th><th>Status</th><th>Docs</th><th>Review queue</th><th>Version</th><th>Trades</th></tr></thead>
          <tbody>{items.map(p => (
            <tr key={p.package_id} aria-selected={selected === p.package_id}>
              <td><button className={selected === p.package_id ? "primary" : ""} onClick={() => onSelect(p.package_id)}>{p.netting_set_id}</button></td>
              <td className="mono">{p.master_id}<br />{p.csa_id}</td>
              <td>{p.party_a_name}<br /><span className="muted">vs</span> {p.party_b_name}</td>
              <td className="mono">{p.base_currency}</td>
              <td><StatusChip tone={STATUS_TONE[p.status] ?? "neutral"}>{p.status.replace(/_/g, " ")}</StatusChip></td>
              <td>{p.documents}{p.docs_with_missing_annexes > 0 && <> <StatusChip tone="danger">missing annex</StatusChip></>}
                {p.docs_pending_extraction > 0 && <> <StatusChip tone="info">{p.docs_pending_extraction} to extract</StatusChip></>}</td>
              <td className="num">{p.outstanding_review > 0 ? <StatusChip tone="warn">{p.outstanding_review} open</StatusChip> : <StatusChip tone="ok">0 open</StatusChip>}</td>
              <td className="num">{p.latest_version_no ? `v${p.latest_version_no}` : "—"}</td>
              <td className="num">{p.trades_in_netting_set}</td>
            </tr>))}</tbody></table></div>}
    </section>
  );
}
