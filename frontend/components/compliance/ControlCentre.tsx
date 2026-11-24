"use client";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { EmptyState, ErrorState, MetricCard, StatusChip } from "@/components/ui";
import { useSession } from "@/lib/session";
import { API, Amount, FieldErrors, Status, fmtTs, useAction, useLoad, type CaseRow, type Evaluation } from "./shared";

interface ControlItem {
  control_key: string; title: string; family: string; rule_key: string; rule_version: number; rule_hash: string; authority_type: string; severity: string;
  frequency: string; max_staleness_hours: number | null; support_status: string; unsupported_reason: string | null; required_inputs: string[];
  evaluated_subjects: number; latest_status_counts: Record<string, number>; last_evaluated_as_of: string | null;
}
interface Coverage {
  controls: ControlItem[];
  coverage: { registered: number; supported: number; unsupported: number; supported_and_evaluated: number; supported_not_evaluated: number;
    subject_evaluations: number; latest_status_counts: Record<string, number>; unknown_count: number; note: string };
  freshness: { latest_evaluation_as_of: string | null };
}
const STATUSES = ["PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE", "CONFLICT", "ERROR"];

export default function ControlCentre({ openCase }: { openCase: (id: string) => void }) {
  const { principal, market } = useSession();
  const { data, error, loading, reload } = useLoad<Coverage>(`${API}/controls`);
  const [family, setFamily] = useState(""); const [status, setStatus] = useState("");
  const seed = useAction();
  const canSeed = !!principal?.roles.some(r => ["tenant_admin", "compliance_officer"].includes(r));

  if (loading) return <EmptyState title="Loading controls…" />;
  if (error) return <ErrorState error={error} />;
  if (!data) return null;
  const c = data.coverage;
  if (c.registered === 0)
    return (
      <EmptyState title="No controls registered for this tenant">
        <p>Controls bind approved rule versions to a population. Register the synthetic control library to begin (all rules are fictional policy).</p>
        {canSeed && <button className="primary" disabled={seed.pending} onClick={async () => { if (await seed.run(`${API}/controls/seed-library`, {})) reload(); }}>
          {seed.pending ? "Seeding…" : "Register synthetic control library"}</button>}
        <FieldErrors error={seed.error} />
      </EmptyState>
    );
  const rows = data.controls.filter(x => (!family || x.family === family) && (!status || (x.latest_status_counts[status] ?? 0) > 0));
  const families = Array.from(new Set(data.controls.map(x => x.family)));
  const fresh = data.freshness.latest_evaluation_as_of;
  return (
    <>
      <div className="card" role="status" style={{ marginBottom: 16, borderColor: fresh ? "var(--border)" : "#6b5420" }}>
        <strong>Freshness: </strong>
        {fresh ? <>latest evaluation as-of <span className="mono">{fmtTs(fresh)}</span> (business time of the evaluation, not the server clock).</>
          : <StatusChip tone="warn">NO EVALUATIONS YET — nothing can be said about compliance</StatusChip>}
      </div>
      <div className="grid c4">
        <MetricCard label="Controls registered" value={c.registered} sub={`${c.supported} supported · ${c.unsupported} unsupported (listed, never counted as passing)`} />
        <MetricCard label="Supported & evaluated" value={`${c.supported_and_evaluated} / ${c.supported}`} sub={`${c.supported_not_evaluated} supported control(s) not yet evaluated`} />
        <MetricCard label="Subject evaluations (latest)" value={c.subject_evaluations} sub="denominator for the status counts below" />
        <MetricCard label="UNKNOWN (never a pass)" value={c.unknown_count} sub="missing, stale or unverifiable inputs" />
      </div>
      <section className="card" style={{ marginTop: 16 }} aria-labelledby="st">
        <h2 id="st">Latest outcome per control × subject — {c.subject_evaluations} evaluation(s)</h2>
        <div className="row">{STATUSES.map(s => <span key={s} className="row" style={{ gap: 6 }}><Status s={s} /><span className="num">{c.latest_status_counts[s] ?? 0}</span></span>)}</div>
        <p className="faint" style={{ marginBottom: 0 }}>{c.note}</p>
      </section>

      <section className="card" style={{ marginTop: 16 }} aria-labelledby="ct">
        <div className="row"><h2 id="ct" style={{ margin: 0 }}>Control registry</h2><span className="spacer" />
          <label className="sr-only" htmlFor="ff">Family</label>
          <select id="ff" value={family} onChange={e => setFamily(e.target.value)} style={{ width: 200 }}>
            <option value="">All families</option>{families.map(f => <option key={f}>{f}</option>)}</select>
          <label className="sr-only" htmlFor="fs">Latest status</label>
          <select id="fs" value={status} onChange={e => setStatus(e.target.value)} style={{ width: 160 }}>
            <option value="">Any status</option>{STATUSES.map(s => <option key={s}>{s}</option>)}</select>
        </div>
        <div className="scroll-x"><table>
          <caption className="sr-only">Controls with rule version, support status and latest outcomes</caption>
          <thead><tr><th>Control</th><th>Rule (source)</th><th>Authority</th><th>Inputs / freshness</th><th>Latest outcomes</th><th>Last as-of</th></tr></thead>
          <tbody>{rows.map(x => (
            <tr key={x.control_key}>
              <td><strong>{x.title}</strong><div className="mono faint">{x.control_key} · {x.family} · FAIL severity {x.severity}</div></td>
              <td className="mono">{x.rule_key} v{x.rule_version}<div className="faint" title={x.rule_hash}>hash {x.rule_hash.slice(0, 10)}…</div></td>
              <td>{x.authority_type === "SYNTHETIC_POLICY" ? <StatusChip tone="info">SYNTHETIC POLICY</StatusChip> : x.authority_type}</td>
              <td>{x.support_status === "UNSUPPORTED"
                ? <><StatusChip tone="warn">UNSUPPORTED</StatusChip><div className="faint">{x.unsupported_reason}</div></>
                : <span className="faint">{x.required_inputs.join(", ")}{x.max_staleness_hours != null ? ` · max age ${x.max_staleness_hours}h` : ""}</span>}</td>
              <td>{x.support_status === "UNSUPPORTED" ? <span className="faint">not evaluated</span> : x.evaluated_subjects === 0 ? <StatusChip tone="warn">UNEVALUATED</StatusChip>
                : <div className="row" style={{ gap: 6 }}>{Object.entries(x.latest_status_counts).map(([s, n]) => <span key={s}><Status s={s} /> <span className="num">{n}</span></span>)}</div>}</td>
              <td className="mono">{fmtTs(x.last_evaluated_as_of)}</td>
            </tr>))}
          </tbody></table></div>
        {rows.length === 0 && <EmptyState title="No controls match these filters" />}
      </section>
      <EvaluateForm market={market} canEvaluate={!!principal?.roles.some(r => ["maker", "compliance_officer", "tenant_admin"].includes(r))} onDone={reload} openCase={openCase} />
      <p className="faint">Drill-through: open <Link href="/compliance?tab=cases">cases</Link> or the <Link href="/compliance?tab=queue">operations queue</Link>. Statuses: PASS / FAIL / UNKNOWN / NOT APPLICABLE / CONFLICT / ERROR. No control outcome is ever generated by a model.</p>
    </>
  );
}

const VENUE: Record<string, [string, string]> = { OTC: ["", "USD"], ETD_DOMESTIC: ["DEMO_NSE", "INR"], ETD_IFSC: ["DEMO_IFSC", "USD"] };

function EvaluateForm({ market, canEvaluate, onDone, openCase }: { market: string; canEvaluate: boolean; onDone: () => void; openCase: (id: string) => void }) {
  const { asOf } = useSession();
  const act = useAction<{ evaluations: Evaluation[]; cases: CaseRow[]; new_evaluations: number }>();
  const [v, c] = VENUE[market] ?? ["", ""];
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget); const g = (k: string) => String(f.get(k) ?? "").trim();
    const facts: Record<string, string> = {};
    for (const k of ["position_lots", "position_basis", "client_classification", "reconciliation_status", "input_as_of"]) if (g(k)) facts[k] = g(k);
    const r = await act.run(`${API}/evaluate`, {
      context: { market_type: market, venue: g("venue") || undefined, account_id: g("account_id"), currency: g("currency") || undefined, as_of: g("as_of") },
      snapshot_id: g("snapshot_id") || null, rule_set_hash: g("rule_set_hash") || null, margin_result_id: g("margin_result_id") || null, facts: Object.keys(facts).length ? facts : null,
    });
    if (r) onDone();
  }
  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="ev">
      <h2 id="ev">Evaluate controls ({market})</h2>
      <p className="muted">Event-triggered evaluation. Coverage amounts come from the risk and collateral engines by id — they cannot be typed in here. Optional caller facts are labelled CALLER_SUPPLIED in the trace.</p>
      <form onSubmit={submit} className="grid c3" key={market} aria-label="Evaluate controls">
        <div><label htmlFor="e-acc">Account / subject</label><input id="e-acc" name="account_id" required defaultValue={market === "ETD_DOMESTIC" ? "ACC-DEMO-NSE-001" : market === "ETD_IFSC" ? "ACC-DEMO-IFSC-001" : "NS-MER-NWD-001"} /></div>
        <div><label htmlFor="e-ven">Venue</label><input id="e-ven" name="venue" defaultValue={v} /></div>
        <div><label htmlFor="e-ccy">Currency</label><input id="e-ccy" name="currency" defaultValue={c} /></div>
        <div><label htmlFor="e-asof">Evaluation as-of (UTC)</label><input id="e-asof" name="as_of" defaultValue={asOf} required /></div>
        <div><label htmlFor="e-snap">Snapshot id</label><input id="e-snap" name="snapshot_id" placeholder="SNAP-ETD-001" /></div>
        <div><label htmlFor="e-mr">Margin result id (from Risk)</label><input id="e-mr" name="margin_result_id" /></div>
        <div><label htmlFor="e-rs">Rule-set hash</label><input id="e-rs" name="rule_set_hash" /></div>
        <div><label htmlFor="e-pl">Position lots (caller-supplied)</label><input id="e-pl" name="position_lots" inputMode="decimal" /></div>
        <div><label htmlFor="e-pb">Position basis</label><input id="e-pb" name="position_basis" placeholder="GROSS_FUTURES_EQUIV" /></div>
        <div><label htmlFor="e-cc">Client classification</label><input id="e-cc" name="client_classification" placeholder="CLIENT" /></div>
        <div><label htmlFor="e-rc">Reconciliation status</label><input id="e-rc" name="reconciliation_status" placeholder="MATCHED" /></div>
        <div><label htmlFor="e-ia">Input as-of (caller-supplied)</label><input id="e-ia" name="input_as_of" placeholder="2026-08-17T03:00:00Z" /></div>
        <div style={{ alignSelf: "end" }}><button className="primary" disabled={!canEvaluate || act.pending}>{act.pending ? "Evaluating…" : "Run evaluation"}</button></div>
      </form>
      {!canEvaluate && <p className="faint">Your role cannot run evaluations (maker, compliance officer or admin required).</p>}
      <FieldErrors error={act.error} />
      {act.result && (
        <div className="scroll-x" style={{ marginTop: 12 }}><table>
          <caption className="sr-only">Evaluation results</caption>
          <thead><tr><th>Control</th><th>Status</th><th>Measured</th><th>Limit</th><th>Gap</th><th>Reasons</th></tr></thead>
          <tbody>{act.result.evaluations.map(e => (
            <tr key={e.evaluation_id}><td className="mono">{e.control_key}</td><td><Status s={e.status} /></td>
              <td><Amount value={e.measured} unit={e.unit} /></td><td><Amount value={e.limit} unit={e.unit} /></td><td><Amount value={e.gap} unit={e.unit} /></td>
              <td className="faint">{e.reasons.join("; ")}{e.case_id && <> · <button className="btn" style={{ minHeight: 28, padding: "0 8px" }} onClick={() => openCase(e.case_id!)}>open case</button></>}</td></tr>))}
          </tbody></table>
          <p className="faint">{act.result.new_evaluations} new evaluation(s); {act.result.cases.length} case(s) opened or updated. A repeated identical event creates nothing new.</p></div>)}
    </section>
  );
}
