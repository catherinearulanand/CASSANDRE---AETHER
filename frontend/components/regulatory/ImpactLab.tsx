"use client";
import { useEffect, useState } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, MoneyDisplay, ProvenanceBadge, StatusChip } from "@/components/ui";
import { fmt, Json, Rule, short, useLoad } from "./shared";

interface Row {
  account_id: string | null; status: string; reason?: string; note?: string; currency?: string; baseline_required?: string; hypothetical_required?: string; delta_required?: string;
  eligible_posted?: string | null; baseline_shortfall?: string; hypothetical_shortfall?: string | null; coverage_note?: string; unknown_rules: string[]; conflicts: string[];
  baseline_rule_set_hash: string; hypothetical_rule_set_hash: string;
  feasibility?: { status: string; reason?: string; solver_status?: string; proposal_state?: string; proposal_id?: string; required_coverage?: string; residual?: string; currency?: string; note?: string };
}
interface Impact {
  impact_id: string; status: string; banner: string; created_at: string; params: { as_of: string; hypothetical_rule_ids: string[] };
  results: { rows: Row[]; counts: Record<string, number>; aggregate_by_currency: Record<string, Record<string, string | number>>; changed_rule_keys: string[]; liquidity_feasibility: string; assumptions: string[]; affected_controls: { note: string } };
}
const SAMPLE: Record<string, string> = {
  ETD_DOMESTIC: JSON.stringify([{ account_id: "ACC-DEMO-NSE-001", context: { market_type: "ETD_DOMESTIC", venue: "DEMO_NSE", currency: "INR" }, snapshot_id: "SNAP-ETD-001", inventory_snapshot_id: "INV-ETD-001", deadline: "2026-08-19T04:00:00Z", horizon_days: 30 }], null, 1),
  ETD_IFSC: JSON.stringify([{ account_id: "ACC-DEMO-IFSC-001", context: { market_type: "ETD_IFSC", venue: "DEMO_IFSC", currency: "USD" }, snapshot_id: "SNAP-IFSC-001", inventory_snapshot_id: "INV-IFSC-001", deadline: "2026-08-19T04:00:00Z", horizon_days: 30 }], null, 1),
  OTC: JSON.stringify([{ account_id: "NS-MER-NWD-001", context: { market_type: "OTC", currency: "USD" }, snapshot_id: "SNAP-SHOCK-001", terms_hash: "<frozen terms hash>" }], null, 1),
};
const TONE: Record<string, "ok" | "warn" | "danger" | "neutral"> = { COMPUTED: "ok", UNAFFECTED: "neutral", BLOCKED: "danger", COMPLETED: "ok", PARTIAL: "warn" };

export default function ImpactLab({ initialImpact }: { initialImpact?: string | null }) {
  const { market, asOf, principal } = useSession();
  const canRun = principal?.roles.some(r => ["maker", "checker", "compliance_officer", "tenant_admin"].includes(r));
  const rules = useLoad(() => api<{ items: Rule[] }>("/regulatory/rules?limit=500"), []);
  const hist = useLoad(() => api<{ items: { impact_id: string; status: string; created_at: string }[] }>("/regulatory/impact"), []);
  const [sel, setSel] = useState<string[]>([]); const [asOfIn, setAsOfIn] = useState(asOf); const [accts, setAccts] = useState(SAMPLE[market]);
  const [feas, setFeas] = useState(false);
  const [res, setRes] = useState<Impact | null>(null); const [err, setErr] = useState<ApiError | string | null>(null); const [busy, setBusy] = useState(false);
  useEffect(() => { setAccts(SAMPLE[market]); setRes(null); }, [market]); // different market => drop stale results
  useEffect(() => { if (initialImpact) api<Impact>(`/regulatory/impact/${initialImpact}`).then(setRes).catch(e => setErr(e)); }, [initialImpact]);

  const candidates = (rules.data?.items ?? []).filter(r => r.compile_ok && r.status !== "SUPERSEDED" && r.status !== "WITHDRAWN" && (r.markets.length === 0 || r.markets.includes(market)));
  async function run() {
    setBusy(true); setErr(null); setRes(null);
    try {
      const r = await api<Impact>("/regulatory/impact", { method: "POST", idempotencyKey: newKey(), body: { as_of: asOfIn, hypothetical_rule_ids: sel, accounts: JSON.parse(accts), include_feasibility: feas } });
      setRes(r); hist.reload();
    } catch (e) { setErr(e instanceof SyntaxError ? `Accounts JSON is invalid: ${e.message}` : (e as ApiError)); } finally { setBusy(false); }
  }
  function download() {
    if (!res) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify({ ...res, _banner: "HYPOTHETICAL - SYNTHETIC DATA - NOT AN OPERATIONAL COMPLIANCE STATUS" }, null, 2)], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = `impact-${res.impact_id.slice(0, 8)}-HYPOTHETICAL.json`; a.click(); URL.revokeObjectURL(url);
  }
  return (
    <>
      <div role="note" className="card" style={{ borderColor: "#6b5420", background: "#2a2412", color: "var(--warn)", marginBottom: 12 }}>
        <strong>HYPOTHETICAL MODE.</strong> Impact runs are counterfactuals on identical pinned snapshots. They are never published as operational compliance status. Margin figures come from the Risk engine; A1 performs no margin arithmetic.
      </div>
      <div className="grid c2" style={{ alignItems: "start" }}>
        <section className="card" aria-labelledby="ic"><h2 id="ic">Scenario controls</h2>
          <fieldset style={{ border: "1px solid var(--border)", borderRadius: 8 }}><legend>Counterfactual rule revisions ({market})</legend>
            {rules.err ? <ErrorState error={rules.err} /> : candidates.length === 0 ? <EmptyState title="No compiling rules for this market" /> : candidates.map(r => (
              <label key={r.rule_id} className="row" style={{ margin: "4px 0", color: "var(--text)" }}><input type="checkbox" style={{ width: 18, minHeight: 18 }} checked={sel.includes(r.rule_id)} onChange={() => setSel(s => (s.includes(r.rule_id) ? s.filter(x => x !== r.rule_id) : [...s, r.rule_id]))} />
                {r.rule_key} v{r.version} <StatusChip tone="neutral">{r.effective_state}</StatusChip><span className="faint mono">eff {fmt(r.effective_from).slice(0, 10)}</span></label>))}
          </fieldset>
          <label htmlFor="asof" style={{ marginTop: 12 }}>As-of (pinned scenario clock, UTC)</label><input id="asof" value={asOfIn} onChange={e => setAsOfIn(e.target.value)} />
          <label htmlFor="acc" style={{ marginTop: 12 }}>Accounts, contexts and pinned snapshot ids (JSON)</label>
          <textarea id="acc" rows={9} className="mono" value={accts} onChange={e => setAccts(e.target.value)} style={{ padding: 8 }} />
          {err && (typeof err === "string" ? <div role="alert" className="field-error">{err}</div> : <div style={{ marginTop: 8 }}><ErrorState error={err} /></div>)}
          <label className="row" style={{ margin: "12px 0 0", color: "var(--text)", flexWrap: "nowrap", alignItems: "flex-start" }}>
            <input type="checkbox" id="feas" style={{ width: 18, minHeight: 18 }} checked={feas} onChange={e => setFeas(e.target.checked)} />
            Also ask the collateral optimiser whether the extra requirement is coverable (creates a HYPOTHETICAL proposal; no asset moves)</label>
          <div className="row" style={{ marginTop: 12 }}><button className="primary" disabled={!canRun || busy || sel.length === 0} onClick={run}>{busy ? "Running…" : "Run counterfactual"}</button>
            {!canRun && <span className="faint">Analyst role required</span>}{sel.length === 0 && <span className="faint">Select at least one revision</span>}</div>
        </section>
        <section className="card" aria-labelledby="ih"><h2 id="ih">Previous runs</h2>
          {!hist.data ? <EmptyState title="Loading…" /> : hist.data.items.length === 0 ? <EmptyState title="No impact runs yet" /> :
            <table><tbody>{hist.data.items.map(h => <tr key={h.impact_id}><td><button style={{ minHeight: 28 }} onClick={() => api<Impact>(`/regulatory/impact/${h.impact_id}`).then(setRes)}>{h.impact_id.slice(0, 8)}</button></td><td><StatusChip tone={TONE[h.status] ?? "neutral"}>{h.status}</StatusChip></td><td className="mono">{fmt(h.created_at)}</td></tr>)}</tbody></table>}
        </section>
      </div>
      {res && (
        <section className="card" style={{ marginTop: 16 }} aria-labelledby="ir" aria-live="polite">
          <div className="row"><h2 id="ir" style={{ margin: 0 }}>Result {res.impact_id.slice(0, 8)}</h2><StatusChip tone={TONE[res.status] ?? "neutral"}>{res.status}</StatusChip><ProvenanceBadge kind="HYPOTHETICAL" /><span className="spacer" /><button onClick={download}>Download impact report (JSON)</button></div>
          <p className="muted">{res.banner}. As-of <span className="mono">{res.params.as_of}</span>. Population: {res.results.counts.accounts} accounts · {res.results.counts.affected} affected · {res.results.counts.unaffected} unaffected (not recomputed) · {res.results.counts.blocked} blocked · {res.results.counts.with_unknown_rules} with unknown-scope rules.</p>
          <div className="scroll-x"><table><thead><tr><th>Account</th><th>Status</th><th className="r">Baseline required</th><th className="r">Counterfactual required</th><th className="r">Delta</th><th className="r">Eligible posted</th><th className="r">Shortfall (counterfactual)</th><th>Notes</th></tr></thead><tbody>
            {res.results.rows.map((r, i) => <tr key={i}><td className="mono">{r.account_id ?? "—"}</td><td><StatusChip tone={TONE[r.status] ?? "neutral"}>{r.status}</StatusChip></td>
              <td className="r">{r.baseline_required ? <MoneyDisplay amount={r.baseline_required} currency={r.currency!} signed={false} /> : "—"}</td>
              <td className="r">{r.hypothetical_required ? <MoneyDisplay amount={r.hypothetical_required} currency={r.currency!} signed={false} /> : "—"}</td>
              <td className="r">{r.delta_required ? <MoneyDisplay amount={r.delta_required} currency={r.currency!} /> : "—"}</td>
              <td className="r">{r.eligible_posted ? <MoneyDisplay amount={r.eligible_posted} currency={r.currency!} signed={false} /> : "—"}</td>
              <td className="r">{r.hypothetical_shortfall != null ? <MoneyDisplay amount={r.hypothetical_shortfall} currency={r.currency!} signed={false} /> : "not computed"}</td>
              <td>{r.reason ?? r.note ?? r.coverage_note ?? ""}{r.unknown_rules.length > 0 && <div><StatusChip tone="warn">unknown scope: {r.unknown_rules.join(", ")}</StatusChip></div>}
                {r.conflicts.length > 0 && <div><StatusChip tone="danger">conflict: {r.conflicts.join(", ")}</StatusChip></div>}
                {r.feasibility && <div data-testid="feasibility" style={{ marginTop: 4 }}>
                  <StatusChip tone={r.feasibility.status === "EVALUATED" && r.feasibility.solver_status === "optimal" ? "ok" : r.feasibility.status === "EVALUATED" ? "warn" : "neutral"}>
                    Coverage feasibility: {r.feasibility.status === "EVALUATED" ? `${r.feasibility.solver_status}, residual ${r.feasibility.residual}` : r.feasibility.status}</StatusChip>
                  <div className="faint">{r.feasibility.reason ?? r.feasibility.note}</div></div>}
                <div className="mono faint">rule sets {short(r.baseline_rule_set_hash)} → {short(r.hypothetical_rule_set_hash)}</div></td></tr>)}
          </tbody></table></div>
          <h3 style={{ marginTop: 16 }}>Aggregates (like currencies only)</h3>
          {Object.keys(res.results.aggregate_by_currency).length === 0 ? <span className="muted">No computed accounts to aggregate (unknowns and blocks are never counted as zero).</span> :
            <table><thead><tr><th>Currency</th><th className="r">Baseline</th><th className="r">Counterfactual</th><th className="r">Delta</th></tr></thead><tbody>{Object.entries(res.results.aggregate_by_currency).map(([c, t]) =>
              <tr key={c}><td>{c}</td><td className="r"><MoneyDisplay amount={String(t.baseline_required)} currency={c} signed={false} /></td><td className="r"><MoneyDisplay amount={String(t.hypothetical_required)} currency={c} signed={false} /></td><td className="r"><MoneyDisplay amount={String(t.delta_required)} currency={c} /></td></tr>)}</tbody></table>}
          <p className="muted">Changed rule keys: <span className="mono">{res.results.changed_rule_keys.join(", ") || "none"}</span>. {res.results.affected_controls.note}.</p>
          <p className="muted">Liquidity feasibility: {res.results.liquidity_feasibility}</p>
          <ul className="muted">{res.results.assumptions.map(a => <li key={a}>{a}</li>)}</ul>
          <details><summary>Raw result</summary><Json v={res.results} /></details>
        </section>)}
    </>
  );
}
