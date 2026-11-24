"use client";
import { useMemo, useState } from "react";
import { EmptyState, ErrorState, ProvenanceBadge, StatusChip } from "@/components/ui";
import { useSession } from "@/lib/session";
import { API, FieldErrors, useAction, useLoad } from "./shared";

interface Metrics {
  sample_size: number; positives: number; prevalence: number | null; precision: number | null; recall: number | null; f1: number | null; pr_auc: number | null;
  false_alerts_per_1000_obs: number | null; confusion_matrix: { tp: number; fp: number; fn: number; tn: number }; episodes: number; episodes_detected: number;
  mean_detection_lag_days: number | null;
}
interface Card {
  name: string; intended_use: string; not_for: string[]; method: string; features: { name: string; definition: string }[]; regime_note: string;
  data: { kind: string; seed: number; accounts: number; days: number; training_data_hash: string; split: Record<string, unknown> };
  limitations: string[]; governance: string;
}
interface Model {
  model_id: string; version: number; status: string; seed: number; threshold: number; note?: string; training_data_hash: string; model_card: Card; disclaimer: string;
  metrics: { primary: { holdout: Metrics; validation: Metrics }; challenger: { name: string; holdout: Metrics }; sensitivity: { threshold: number; multiple_of_selected: number; precision: number | null; recall: number | null; false_alerts_per_1000_obs: number }[]; drift_ratio: number; drift_suspected: boolean };
}
interface Score { account_id: string; obs_day: number; score: number; flagged: boolean; top_features: { feature: string; value: number; baseline_median: number; robust_z: number }[] }
interface View { model: Model | null; scores: Score[]; series: Score[]; disclaimer: string; note?: string }

const pct = (x: number | null) => (x == null ? "n/a" : (x * 100).toFixed(1) + "%");
const num = (x: number | null, d = 3) => (x == null ? "n/a" : x.toFixed(d));

export default function SurveillanceView({ openCase }: { openCase: () => void }) {
  const { principal } = useSession();
  const { data, error, loading, reload } = useLoad<View>(`${API}/surveillance?limit=40`);
  const run = useAction<{ cases_opened: number }>();
  const can = !!principal?.roles.some(r => ["compliance_officer", "tenant_admin"].includes(r));
  const [acct, setAcct] = useState("");
  const accounts = useMemo(() => Array.from(new Set((data?.series ?? []).map(s => s.account_id))).sort(), [data]);
  if (loading) return <EmptyState title="Loading surveillance…" />;
  if (error) return <ErrorState error={error} />;
  if (!data) return null;
  const m = data.model;
  return (
    <>
      <div role="note" className="state" style={{ textAlign: "left", borderStyle: "solid", marginBottom: 16 }}>
        <StatusChip tone="info">INVESTIGATION SIGNAL ONLY</StatusChip> {data.disclaimer} Scores rank <em>synthetic</em> account-days for human review; they carry no accusation and never alter a control result.
      </div>
      {!m ? (
        <EmptyState title="No surveillance model trained yet">
          <p>{data.note}</p>
          {can ? <button className="primary" disabled={run.pending} onClick={async () => { if (await run.run(`${API}/surveillance/run`, { seed: 42 })) reload(); }}>
            {run.pending ? "Training on synthetic data…" : "Train on synthetic data (seed 42)"}</button> : <p className="faint">A compliance officer or admin can train the model.</p>}
          <FieldErrors error={run.error} />
        </EmptyState>
      ) : (
        <>
          <div className="row" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>Model v{m.version}</h2><StatusChip tone={m.status === "ACTIVE" ? "ok" : "warn"}>{m.status}</StatusChip><ProvenanceBadge kind="SYNTHETIC" />
            <span className="mono faint">seed {m.seed} · training data hash {m.training_data_hash.slice(0, 12)}…</span><span className="spacer" />
            {can && <button disabled={run.pending} onClick={async () => { if (await run.run(`${API}/surveillance/run`, { seed: 42 })) reload(); }}>{run.pending ? "Retraining…" : "Retrain (new version)"}</button>}
          </div>
          <FieldErrors error={run.error} />
          {m.note && <p><StatusChip tone="warn">{m.note}</StatusChip></p>}
          {m.metrics.drift_suspected && <p><StatusChip tone="warn">DRIFT SUSPECTED (score median ratio {num(m.metrics.drift_ratio, 2)}) — simple monitor, not a validated test</StatusChip></p>}
          <div className="grid c2">
            <section className="card" aria-labelledby="mt">
              <h2 id="mt">Chronological holdout results</h2>
              <p className="muted">Split: {String((m.model_card.data.split as Record<string, unknown>).scheme)}; threshold chosen on validation only. Labels are injected synthetic anomalies — this is not real-world accuracy.</p>
              <div className="scroll-x"><table><caption className="sr-only">Holdout metrics for primary model and challenger</caption>
                <thead><tr><th>Metric</th><th className="r">Robust MAD (operational)</th><th className="r">IsolationForest (challenger)</th></tr></thead>
                <tbody>
                  {([["Sample size (obs)", (x: Metrics) => String(x.sample_size)], ["Positives / prevalence", (x: Metrics) => `${x.positives} / ${pct(x.prevalence)}`],
                    ["Precision", (x: Metrics) => pct(x.precision)], ["Recall", (x: Metrics) => pct(x.recall)], ["PR-AUC", (x: Metrics) => num(x.pr_auc)],
                    ["False alerts / 1,000 obs", (x: Metrics) => num(x.false_alerts_per_1000_obs, 2)],
                    ["Episodes detected", (x: Metrics) => `${x.episodes_detected} / ${x.episodes}`], ["Mean detection lag (days)", (x: Metrics) => num(x.mean_detection_lag_days, 2)]] as [string, (x: Metrics) => string][])
                    .map(([l, f]) => <tr key={l}><td>{l}</td><td className="r num">{f(m.metrics.primary.holdout)}</td><td className="r num">{f(m.metrics.challenger.holdout)}</td></tr>)}
                </tbody></table></div>
              <h3 style={{ marginTop: 16 }}>Confusion matrix (primary, holdout)</h3>
              <table><caption className="sr-only">Confusion matrix</caption><thead><tr><th></th><th className="r">Flagged</th><th className="r">Not flagged</th></tr></thead><tbody>
                <tr><th scope="row">Injected anomaly</th><td className="r num">{m.metrics.primary.holdout.confusion_matrix.tp} (true +)</td><td className="r num">{m.metrics.primary.holdout.confusion_matrix.fn} (missed)</td></tr>
                <tr><th scope="row">Normal</th><td className="r num">{m.metrics.primary.holdout.confusion_matrix.fp} (false +)</td><td className="r num">{m.metrics.primary.holdout.confusion_matrix.tn} (true −)</td></tr></tbody></table>
            </section>
            <section className="card" aria-labelledby="ts">
              <h2 id="ts">Threshold sensitivity (holdout)</h2>
              <table><caption className="sr-only">Precision and recall at different thresholds</caption>
                <thead><tr><th className="r">Threshold (score)</th><th className="r">× selected</th><th className="r">Precision</th><th className="r">Recall</th><th className="r">False alerts / 1,000</th></tr></thead>
                <tbody>{m.metrics.sensitivity.map(s => <tr key={s.multiple_of_selected} style={s.multiple_of_selected === 1 ? { background: "var(--accent-wash)" } : undefined}>
                  <td className="r num">{num(s.threshold, 2)}</td><td className="r num">{s.multiple_of_selected}{s.multiple_of_selected === 1 ? " (selected)" : ""}</td><td className="r num">{pct(s.precision)}</td><td className="r num">{pct(s.recall)}</td><td className="r num">{num(s.false_alerts_per_1000_obs, 2)}</td></tr>)}</tbody></table>
              <p className="faint">Lower thresholds find more injected anomalies at the cost of more false alerts per 1,000 observations. Small sample (see sample size): differences between models are within noise.</p>
            </section>
          </div>
          <section className="card" style={{ marginTop: 16 }} aria-labelledby="ch">
            <h2 id="ch">Score history (synthetic series)</h2>
            <div style={{ maxWidth: 260 }}><label htmlFor="sa">Account</label><select id="sa" value={acct} onChange={e => setAcct(e.target.value)}><option value="">Select…</option>{accounts.map(a => <option key={a}>{a}</option>)}</select></div>
            {acct ? <ScoreChart rows={data.series.filter(s => s.account_id === acct)} threshold={m.threshold} /> : <p className="faint">Select an account to see its validation/holdout score series and the threshold.</p>}
          </section>
          <section className="card" style={{ marginTop: 16 }} aria-labelledby="tp">
            <h2 id="tp">Highest-ranked holdout observations — feature deviations</h2>
            <div className="scroll-x"><table><caption className="sr-only">Top scored observations with transparent feature deviations</caption>
              <thead><tr><th>Account</th><th className="r">Day</th><th className="r">Score</th><th>Flagged</th><th>Largest feature deviations (robust z vs own baseline)</th></tr></thead>
              <tbody>{data.scores.map(s => <tr key={`${s.account_id}-${s.obs_day}`}><td className="mono">{s.account_id}</td><td className="r num">{s.obs_day}</td><td className="r num">{num(s.score, 2)}</td>
                <td>{s.flagged ? <StatusChip tone="info">RANKED HIGH</StatusChip> : <span className="faint">below threshold</span>}</td>
                <td>{s.top_features.length ? s.top_features.map(f => `${f.feature}: ${f.value} vs median ${f.baseline_median} (z ${f.robust_z})`).join(" · ") : <span className="faint">not computed (below threshold)</span>}</td></tr>)}</tbody></table></div>
            <p className="faint">Explanations are plain deviations from the account’s own baseline — they do not establish cause or intent. <button className="btn" onClick={openCase} style={{ minHeight: 28 }}>Open the resulting investigation cases</button></p>
          </section>
          <div className="grid c2" style={{ marginTop: 16 }}>
            <section className="card" aria-labelledby="mc"><h2 id="mc">Model card</h2>
              <p><strong>Intended use:</strong> {m.model_card.intended_use}</p><p><strong>Not for:</strong> {m.model_card.not_for.join("; ")}.</p>
              <p><strong>Method:</strong> {m.model_card.method}</p><p className="faint">{m.model_card.regime_note}</p>
              <details><summary>Feature definitions ({m.model_card.features.length})</summary><ul>{m.model_card.features.map(f => <li key={f.name}><span className="mono">{f.name}</span> — {f.definition}</li>)}</ul></details>
              <p className="faint">{m.model_card.governance}</p></section>
            <section className="card" aria-labelledby="ml"><h2 id="ml">Limits you must read</h2><ul>{m.model_card.limitations.map(l => <li key={l}>{l}</li>)}</ul></section>
          </div>
        </>)}
    </>
  );
}

/** Accessible SVG line chart: labelled axes, threshold line, plus a data table alternative. */
function ScoreChart({ rows, threshold }: { rows: Score[]; threshold: number }) {
  const W = 640, H = 200, P = 36;
  const xs = rows.map(r => r.obs_day); const lo = Math.min(...xs), hi = Math.max(...xs);
  const ymax = Math.max(threshold * 1.2, ...rows.map(r => r.score));
  const X = (d: number) => P + ((d - lo) / Math.max(1, hi - lo)) * (W - 2 * P); const Y = (v: number) => H - P - (v / ymax) * (H - 2 * P);
  const path = rows.map((r, i) => `${i ? "L" : "M"}${X(r.obs_day).toFixed(1)},${Y(r.score).toFixed(1)}`).join(" ");
  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Anomaly score by observation day, threshold ${threshold.toFixed(2)}`} style={{ width: "100%", maxWidth: 720 }}>
        <line x1={P} y1={H - P} x2={W - P} y2={H - P} stroke="var(--border)" /><line x1={P} y1={P} x2={P} y2={H - P} stroke="var(--border)" />
        <line x1={P} y1={Y(threshold)} x2={W - P} y2={Y(threshold)} stroke="var(--warn)" strokeDasharray="4 3" />
        <text x={W - P} y={Y(threshold) - 4} fill="var(--warn)" fontSize="11" textAnchor="end">threshold {threshold.toFixed(2)}</text>
        <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
        {rows.filter(r => r.flagged).map(r => <circle key={r.obs_day} cx={X(r.obs_day)} cy={Y(r.score)} r="4" fill="var(--info)"><title>day {r.obs_day}: score {r.score.toFixed(2)} (ranked high)</title></circle>)}
        <text x={W / 2} y={H - 6} fill="var(--text-2)" fontSize="11" textAnchor="middle">observation day (validation then holdout)</text>
        <text x={12} y={H / 2} fill="var(--text-2)" fontSize="11" transform={`rotate(-90 12 ${H / 2})`} textAnchor="middle">max |robust z|</text>
      </svg>
      <details><summary>Data table alternative ({rows.length} observations)</summary>
        <div className="scroll-x" style={{ maxHeight: 240, overflowY: "auto" }}><table><thead><tr><th className="r">Day</th><th className="r">Score</th><th>Ranked high</th></tr></thead>
          <tbody>{rows.map(r => <tr key={r.obs_day}><td className="r num">{r.obs_day}</td><td className="r num">{r.score.toFixed(2)}</td><td>{r.flagged ? "yes" : "no"}</td></tr>)}</tbody></table></div></details>
    </>
  );
}
