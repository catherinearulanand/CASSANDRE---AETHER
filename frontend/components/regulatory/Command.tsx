"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, MetricCard, StatusChip } from "@/components/ui";
import { fmt, Rule, RuleStatus, short, useLoad } from "./shared";

interface Summary {
  rule_counts: Record<string, number>; pending_approvals: number; upcoming_changes: { rule_id: string; rule_key: string; version: number; effective_from: string }[];
  source_freshness: { authority_type: string; documents: number; latest_retrieved_at: string | null }[]; affected_accounts: number | null;
  latest_impact: { impact_id: string; status: string; counts: Record<string, number> } | null; as_of_server_time: string; data_mode: string;
}
const AUTH = ["", "SYNTHETIC_POLICY", "INTERNAL_POLICY", "REGULATORY_VERIFIED"]; const STATUS = ["", "ACTIVE", "SCHEDULED", "APPROVED", "REVIEW_REQUIRED", "DRAFT", "SUPERSEDED", "WITHDRAWN"];

export default function Command({ openRule, openImpact }: { openRule: (id: string) => void; openImpact: (id: string) => void }) {
  const { market } = useSession();
  const [auth, setAuth] = useState(""); const [status, setStatus] = useState(""); const [onlyMarket, setOnlyMarket] = useState(true);
  const sum = useLoad(() => api<Summary>("/regulatory/summary"), []);
  const q = new URLSearchParams(); if (auth) q.set("authority_type", auth); if (status) q.set("status", status); if (onlyMarket) q.set("market", market === "OTC" ? "OTC" : market);
  const rules = useLoad(() => api<{ items: Rule[] }>(`/regulatory/rules?${q}`), [auth, status, onlyMarket, market]);
  const c = sum.data?.rule_counts ?? {};
  return (
    <>
      {sum.err ? <ErrorState error={sum.err} /> : (
        <div className="grid c4" aria-label="Regulatory summary">
          <MetricCard label="Active" value={sum.data ? String(c.ACTIVE ?? 0) : "…"} sub={`${c.SUPERSEDED ?? 0} superseded · ${c.WITHDRAWN ?? 0} withdrawn`} />
          <MetricCard label="Scheduled" value={sum.data ? String(c.SCHEDULED ?? 0) : "…"} sub="approved, effective in the future" />
          <MetricCard label="Draft / in review" value={sum.data ? `${c.DRAFT ?? 0} / ${c.REVIEW_REQUIRED ?? 0}` : "…"} sub={`${sum.data?.pending_approvals ?? "…"} pending approvals`} />
          <MetricCard label="Affected accounts (latest impact)" value={sum.data ? (sum.data.affected_accounts == null ? "no impact run" : String(sum.data.affected_accounts)) : "…"}
            sub={sum.data?.latest_impact ? <a href="#impact" onClick={e => { e.preventDefault(); openImpact(sum.data!.latest_impact!.impact_id); }}>HYPOTHETICAL · {sum.data.latest_impact.status}</a> : "run the Impact lab"} />
        </div>)}
      <div className="grid c2" style={{ marginTop: 16 }}>
        <section className="card" aria-labelledby="up"><h2 id="up">Upcoming changes</h2>
          {!sum.data ? <EmptyState title="Loading…" /> : sum.data.upcoming_changes.length === 0 ? <EmptyState title="No scheduled changes" /> :
            <table><thead><tr><th>Rule</th><th>Effective from</th></tr></thead><tbody>{sum.data.upcoming_changes.map(u => <tr key={u.rule_id}><td><button style={{ minHeight: 28 }} onClick={() => openRule(u.rule_id)}>{u.rule_key} v{u.version}</button></td><td className="mono">{fmt(u.effective_from)}</td></tr>)}</tbody></table>}
        </section>
        <section className="card" aria-labelledby="fr"><h2 id="fr">Source freshness</h2>
          {!sum.data ? <EmptyState title="Loading…" /> : sum.data.source_freshness.length === 0 ? <EmptyState title="No sources ingested" /> :
            <table><thead><tr><th>Authority class</th><th className="r">Docs</th><th>Latest retrieved</th></tr></thead><tbody>{sum.data.source_freshness.map(s => <tr key={s.authority_type}><td>{s.authority_type}</td><td className="r num">{s.documents}</td><td className="mono">{fmt(s.latest_retrieved_at)}</td></tr>)}</tbody></table>}
          <p className="faint" style={{ marginBottom: 0 }}>Freshness is retrieval time of ingested synthetic documents; no live regulatory feed exists in this build.</p>
        </section>
      </div>
      <section className="card" style={{ marginTop: 16 }} aria-labelledby="rl">
        <div className="row"><h2 id="rl" style={{ margin: 0 }}>Rule registry</h2><span className="spacer" />
          <label className="sr-only" htmlFor="fa">Authority</label><select id="fa" value={auth} onChange={e => setAuth(e.target.value)} style={{ width: 190 }}>{AUTH.map(a => <option key={a} value={a}>{a || "All authority types"}</option>)}</select>
          <label className="sr-only" htmlFor="fs">Status</label><select id="fs" value={status} onChange={e => setStatus(e.target.value)} style={{ width: 170 }}>{STATUS.map(a => <option key={a} value={a}>{a || "All statuses"}</option>)}</select>
          <label className="row" style={{ margin: 0 }}><input type="checkbox" checked={onlyMarket} onChange={e => setOnlyMarket(e.target.checked)} style={{ width: 18, minHeight: 18 }} /> only {market}</label></div>
        <div style={{ marginTop: 12 }}>
          {rules.err ? <ErrorState error={rules.err} /> : !rules.data ? <EmptyState title="Loading rules…" /> : rules.data.items.length === 0 ? <EmptyState title="No rules match">Adjust filters, or seed the synthetic fixtures (<span className="mono">python -m app.engines.regulatory.seed</span>).</EmptyState> :
            <div className="scroll-x"><table><thead><tr><th>Rule</th><th>Status</th><th>Authority</th><th>Markets</th><th>Effective from</th><th>Hash</th></tr></thead><tbody>
              {rules.data.items.map(r => <tr key={r.rule_id}>
                <td><button onClick={() => openRule(r.rule_id)} style={{ minHeight: 32 }}>{r.rule_key} v{r.version}</button><div className="faint">{r.title}</div></td>
                <td><RuleStatus s={r.effective_state} />{!r.compile_ok && <StatusChip tone="danger">no compile</StatusChip>}</td>
                <td>{r.authority_type}</td><td>{r.markets.join(", ") || "—"}</td><td className="mono">{fmt(r.effective_from)}</td><td className="mono">{short(r.content_hash)}</td></tr>)}
            </tbody></table></div>}
        </div>
      </section>
    </>
  );
}
