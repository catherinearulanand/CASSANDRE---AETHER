"use client";
import { useEffect, useState } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import type { Membership, PkgDetail, TermsView } from "./types";

const CAP = { OBLIGATION: "of margin obligation", TOTAL_EFFECTIVE_ALLOCATED: "of total effective allocated coverage" } as Record<string, string>;
const TONE = { MEMBER: "ok", QUARANTINED: "warn", REJECTED: "danger" } as const;

export default function EligibilityNetting({ pkg, terms }: { pkg: PkgDetail; terms: TermsView }) {
  const { principal } = useSession();
  const isMaker = !!principal?.roles.includes("maker");
  const [members, setMembers] = useState<Membership[] | null>(null);
  const [quar, setQuar] = useState<Membership[]>([]);
  const [err, setErr] = useState<ApiError | null>(null);
  const [form, setForm] = useState({ trade_id: "", declared_master_id: pkg.master_id, declared_netting_set_id: pkg.netting_set_id });
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [m, q] = await Promise.all([api<{ items: Membership[] }>(`/contracts/${pkg.package_id}/netting`), api<{ items: Membership[] }>(`/contracts/netting/quarantine`)]);
      setMembers(m.items); setQuar(q.items); setErr(null);
    } catch (e) { setErr(e as ApiError); }
  }
  useEffect(() => { setMembers(null); load(); }, [pkg.package_id, pkg.revision]); // eslint-disable-line react-hooks/exhaustive-deps

  async function register() {
    setBusy(true);
    try { await api("/contracts/netting/import", { method: "POST", idempotencyKey: newKey(), body: { trades: [{ ...form, declared_netting_set_id: form.declared_netting_set_id || null }] } }); setForm({ ...form, trade_id: "" }); await load(); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }
  const rows = terms.eligibility.filter(r => r.field_status !== "REJECTED");

  return (
    <div className="grid" style={{ gap: 16 }}>
      <section className="card" aria-labelledby="el-h">
        <h2 id="el-h">Eligible collateral schedule</h2>
        {rows.length === 0 ? <EmptyState title="No eligibility rows yet">Extract the CSA and review the eligible-collateral field.</EmptyState> :
          <div className="scroll-x" role="region" tabIndex={0} aria-label="Eligible collateral schedule (scrollable)"><table>
            <caption className="sr-only">Eligible collateral with valuation percentage, haircut and FX haircut shown separately</caption>
            <thead><tr><th>Asset</th><th>Ccy</th><th>Issuer</th><th className="r">Valuation %</th><th className="r">Haircut %</th><th className="r">FX haircut %</th><th>Concentration cap</th><th>Min rating</th><th className="r">Max maturity (y)</th><th>Status</th></tr></thead>
            <tbody>{rows.map((r, i) => (
              <tr key={`${r.field_id}-${i}`}><td>{r.asset_class}</td><td className="mono">{r.currency}</td><td>{r.issuer_class}</td>
                <td className="num r">{r.valuation_pct}</td><td className="num r">{r.haircut_pct}</td><td className="num r">{r.fx_haircut_pct}</td>
                <td>{r.concentration_cap_pct ? <span className="num">{r.concentration_cap_pct}% <span className="muted">{CAP[r.cap_denominator ?? ""] ?? r.cap_denominator}</span></span> : "none"}</td>
                <td>{r.min_rating ?? "none"}</td><td className="num r">{r.max_maturity_years ?? "none"}</td>
                <td><StatusChip tone={r.field_status === "APPROVED" ? "ok" : "warn"}>{r.field_status}</StatusChip></td></tr>))}</tbody></table></div>}
        <p className="faint">Valuation % and Haircut % are two views of ONE discount (they must sum to 100) — never apply both. The FX haircut is a separate, additive reviewed factor. The cap denominator states what the cap is measured against.</p>
      </section>

      <section className="card" aria-labelledby="nm-h">
        <h2 id="nm-h">Netting map — {pkg.netting_set_id}</h2>
        <p className="muted">Governed by master <span className="mono">{pkg.master_id}</span> / <span className="mono">{pkg.csa_id}</span>. Netting is only ever within one netting set of one master; there is no cross-master offset.</p>
        {err && <ErrorState error={err} />}
        {!members ? <EmptyState title="Loading membership…" /> : members.length === 0 ? <EmptyState title="No trades mapped to this netting set yet" /> :
          <div className="scroll-x" role="region" tabIndex={0} aria-label="Netting map trades (scrollable)"><table><caption className="sr-only">Trades and why they belong to this netting set</caption>
            <thead><tr><th>Trade</th><th>Declared master / set</th><th>Result</th><th>Why</th></tr></thead>
            <tbody>{members.map(m => <tr key={m.trade_id}><td className="mono">{m.trade_id}</td><td className="mono">{m.declared_master_id}<br />{m.declared_netting_set_id}</td>
              <td><StatusChip tone={TONE[m.status as keyof typeof TONE] ?? "neutral"}>{m.status}</StatusChip></td><td>{m.reason}</td></tr>)}</tbody></table></div>}
        <h3 style={{ marginTop: 16 }}>Register trade mapping</h3>
        <div className="grid c3">
          <div><label htmlFor="t-id">Trade ID</label><input id="t-id" value={form.trade_id} onChange={e => setForm({ ...form, trade_id: e.target.value })} /></div>
          <div><label htmlFor="t-m">Declared master</label><input id="t-m" value={form.declared_master_id} onChange={e => setForm({ ...form, declared_master_id: e.target.value })} /></div>
          <div><label htmlFor="t-n">Declared netting set</label><input id="t-n" value={form.declared_netting_set_id} onChange={e => setForm({ ...form, declared_netting_set_id: e.target.value })} /></div></div>
        <button style={{ marginTop: 8 }} disabled={busy || !isMaker || !form.trade_id} onClick={register} title={isMaker ? "" : "Requires maker role"}>Register</button>
        <h3 style={{ marginTop: 16 }}>Quarantine / rejected (tenant-wide)</h3>
        {quar.length === 0 ? <p className="muted">None.</p> : <ul>{quar.map(m => <li key={m.trade_id}><span className="mono">{m.trade_id}</span> <StatusChip tone={TONE[m.status as keyof typeof TONE] ?? "neutral"}>{m.status}</StatusChip> {m.reason}</li>)}</ul>}
        <p className="faint">Quarantined trades are excluded from every calculation until the mapping is resolved; unknown mappings are never defaulted into a set.</p>
      </section>
    </div>
  );
}
