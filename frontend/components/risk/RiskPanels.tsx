"use client";
import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { PINNED_AS_OF, useSession } from "@/lib/session";
import { EmptyState, ErrorState, MetricCard, MoneyDisplay, ProvenanceBadge, StatusChip } from "@/components/ui";
import {
  DIRECTION_TEXT, ETD_ACCOUNT, METHOD_FOR, type ClearingFile, type MarginResult, type Methodology, type ResultRow, type Snapshot, type StressOut,
} from "./types";

type Tone = "ok" | "warn" | "danger" | "info" | "neutral";
const statusTone = (s: string): Tone => (s === "OK" ? "ok" : s === "UNSUPPORTED" ? "warn" : "danger");

/** Shared state: snapshots, latest result for the selected context; query key includes market, stale data cleared on change. */
export function useRiskContext() {
  const { market, principal } = useSession();
  const [snaps, setSnaps] = useState<Snapshot[] | null>(null);
  const [files, setFiles] = useState<ClearingFile[]>([]);
  const [nsets, setNsets] = useState<string[]>([]);
  const [results, setResults] = useState<ResultRow[]>([]);
  const [result, setResult] = useState<MarginResult | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const canWrite = !!principal?.roles.some(r => ["maker", "compliance_officer", "tenant_admin"].includes(r));

  const reload = useCallback(async () => {
    try {
      const [s, c, t, r] = await Promise.all([
        api<{ items: Snapshot[] }>("/risk/snapshots"), api<{ items: ClearingFile[] }>("/risk/clearing"),
        api<{ items: { netting_set_id: string | null }[] }>("/risk/trades"), api<{ items: ResultRow[] }>(`/risk/results?market_type=${market}`),
      ]);
      setSnaps(s.items); setFiles(c.items); setResults(r.items);
      setNsets([...new Set(t.items.map(x => x.netting_set_id).filter((x): x is string => !!x))].sort());
      setErr(null);
    } catch (e) { setErr(e as ApiError); }
  }, [market]);
  useEffect(() => { setSnaps(null); setResult(null); setResults([]); reload(); }, [market, reload]);
  return { market, snaps, files, nsets, results, result, setResult, err, setErr, reload, canWrite };
}
export type RiskCtx = ReturnType<typeof useRiskContext>;

function Badges({ r }: { r: MarginResult }) {
  return (
    <div className="row" aria-label="Method and provenance">
      <StatusChip tone="info">{r.method} · {r.method_version}</StatusChip>
      {r.provenance_flags.map(f => <ProvenanceBadge key={f} kind={f === "SYNTHETIC" ? "SYNTHETIC" : "HYPOTHETICAL"} />).slice(0, 1)}
      {r.hypothetical && <ProvenanceBadge kind="HYPOTHETICAL" />}
      {r.provenance_flags.filter(f => !["SYNTHETIC", "HYPOTHETICAL_RULE_PARAMETER", "HYPOTHETICAL_IM_SCHEDULE"].includes(f)).map(f => <StatusChip key={f} tone="neutral">{f}</StatusChip>)}
    </div>
  );
}

export function ResultSummary({ r }: { r: MarginResult }) {
  return (
    <section className="card" aria-labelledby="res-h" style={{ marginTop: 16 }}>
      <div className="row"><h2 id="res-h" style={{ margin: 0 }}>Margin result</h2><StatusChip tone={statusTone(r.status)}>{r.status}</StatusChip><span className="spacer" />
        <span className="mono faint">result {r.result_id.slice(0, 8)} · hash {r.result_hash.slice(0, 12)}</span>
        <Link href={`/evidence?entity=${r.result_id}`}>Evidence →</Link></div>
      <Badges r={r} />
      {r.status !== "OK" && (
        <div className="state error" role="alert" style={{ marginTop: 12, textAlign: "left" }}>
          <strong>{r.status === "UNSUPPORTED" ? "Unsupported market type" : "Blocked — no authoritative figure is shown"}</strong>
          <ul>{r.blocking_reasons.map((b, i) => <li key={i}>{b}</li>)}</ul>
        </div>
      )}
      {r.status === "OK" && (
        <div className="grid c4" style={{ marginTop: 12 }}>
          <MetricCard label={r.market_type === "OTC" ? "Net exposure (signed)" : "Imported margin obligation"} value={r.exposure && <MoneyDisplay {...r.exposure} />} sub="exact amount; sign explicit" />
          <MetricCard label="Required transfer / margin" value={r.required_amount && <MoneyDisplay {...r.required_amount} signed={false} />} sub={DIRECTION_TEXT[r.direction]} />
          <MetricCard label="Unrounded → rounded" value={r.unrounded_amount && r.rounded_amount && <><MoneyDisplay {...r.unrounded_amount} signed={false} /><br /><MoneyDisplay {...r.rounded_amount} signed={false} /></>} sub="MTA tested on the unrounded amount" />
          <MetricCard label="Pinned inputs" value={<span className="mono" style={{ fontSize: 13 }}>{r.snapshot_id}</span>} sub={`terms ${r.terms_hash?.slice(0, 10) ?? "n/a"} · rules ${r.rule_set_hash?.slice(0, 10) ?? "none pinned"}`} />
        </div>
      )}
      {r.warnings.length > 0 && <ul className="muted" aria-label="Warnings">{r.warnings.map((w, i) => <li key={i}>⚠ {w}</li>)}</ul>}
      {Object.keys(r.quality).length > 0 && (
        <p className="mono faint">quality: {Object.entries(r.quality).map(([k, v]) => `${k}=${String(v)}`).join(" · ")}</p>
      )}
    </section>
  );
}

export function RiskDesk({ ctx }: { ctx: RiskCtx }) {
  const { market, snaps, nsets, files, canWrite, result, setResult, setErr, err, results, reload } = ctx;
  const [snap, setSnap] = useState(""); const [ns, setNs] = useState(""); const [held, setHeld] = useState({ A: "0", B: "0" });
  const [mult, setMult] = useState(""); const [termsInfo, setTermsInfo] = useState<{ status: string; terms_hash: string | null; blocking_reasons: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setSnap(""); setNs(nsets[0] ?? ""); setTermsInfo(null); }, [market, nsets]);
  const isOtc = market === "OTC";
  const relevantSnaps = (snaps ?? []).filter(s => (isOtc ? /QUIET|SHOCK|STALE|G08/.test(s.snapshot_id) : market === "ETD_IFSC" ? s.snapshot_id.includes("IFSC") : s.snapshot_id.includes("ETD")));

  async function resolveTerms() {
    setBusy(true);
    try { setTermsInfo(await api("/risk/terms/resolve", { method: "POST", body: { netting_set_id: ns, as_of: PINNED_AS_OF } })); setErr(null); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }
  async function run(e: FormEvent) {
    e.preventDefault(); setBusy(true);
    const context: Record<string, unknown> = isOtc
      ? { market_type: "OTC", netting_set_id: ns, as_of: PINNED_AS_OF, existing_vm_balance: { PARTY_A: held.A, PARTY_B: held.B } }
      : { market_type: market, account_id: ETD_ACCOUNT[market].account, venue: ETD_ACCOUNT[market].venue, as_of: PINNED_AS_OF, ...(mult ? { hypothetical_margin_multiplier: mult } : {}) };
    try {
      const r = await api<MarginResult>("/risk/calculate", { method: "POST", idempotencyKey: newKey(),
        body: { context, snapshot_id: snap || null, terms_hash: isOtc ? termsInfo?.terms_hash ?? null : null, rule_set_hash: null, methodology: METHOD_FOR[market] } });
      setResult(r); setErr(null); reload();
    } catch (x) { setErr(x as ApiError); } finally { setBusy(false); }
  }
  if (!snaps) return err ? <ErrorState error={err} /> : <EmptyState title="Loading pinned snapshots…" />;
  return (
    <>
      <form className="card" onSubmit={run} aria-label="Run margin calculation">
        <h2>Risk desk — {isOtc ? "OTC bilateral (illustrative VM waterfall)" : market === "ETD_IFSC" ? "IFSC ETD · USD profile" : "Domestic ETD · INR profile"}</h2>
        <div className="grid c3">
          <div><label htmlFor="snap">Pinned market snapshot</label>
            <select id="snap" value={snap} onChange={e => setSnap(e.target.value)} required>
              <option value="">— select —</option>{relevantSnaps.map(s => <option key={s.snapshot_id} value={s.snapshot_id}>{s.snapshot_id} · data as of {s.data_as_of.slice(0, 16)}Z</option>)}</select>
            {relevantSnaps.length === 0 && <div className="field-error">No snapshot for this market. Pin one via the API.</div>}</div>
          {isOtc ? <>
            <div><label htmlFor="ns">Netting set</label><select id="ns" value={ns} onChange={e => { setNs(e.target.value); setTermsInfo(null); }}>{nsets.map(n => <option key={n}>{n}</option>)}</select></div>
            <div><label>Existing VM collateral held (explicit; no silent default)</label>
              <div className="row"><input aria-label="Held from Party A" value={held.A} onChange={e => setHeld({ ...held, A: e.target.value })} inputMode="decimal" style={{ width: 110 }} />
                <input aria-label="Held from Party B" value={held.B} onChange={e => setHeld({ ...held, B: e.target.value })} inputMode="decimal" style={{ width: 110 }} /></div>
              <span className="faint">Party A · Party B ({"USD"})</span></div>
          </> : <>
            <div><label>Account / venue</label><div className="mono">{ETD_ACCOUNT[market].account} · {ETD_ACCOUNT[market].venue}</div></div>
            <div><label htmlFor="mult">Hypothetical margin multiplier (optional)</label><input id="mult" value={mult} onChange={e => setMult(e.target.value)} placeholder="e.g. 1.25" inputMode="decimal" />
              <span className="faint">Marked HYPOTHETICAL; never presented as an official rule.</span></div>
          </>}
        </div>
        {isOtc && (
          <div className="row" style={{ marginTop: 12 }}>
            <button type="button" onClick={resolveTerms} disabled={busy || !ns}>Resolve approved terms</button>
            {termsInfo && (termsInfo.status === "RESOLVED"
              ? <StatusChip tone="ok">terms hash {termsInfo.terms_hash?.slice(0, 12)}</StatusChip>
              : <StatusChip tone="danger">BLOCKED: {termsInfo.blocking_reasons.join("; ") || "terms unavailable"}</StatusChip>)}
          </div>)}
        <div className="row" style={{ marginTop: 16 }}>
          <button className="primary" disabled={busy || !canWrite || !snap || (isOtc && !ns)} title={canWrite ? "" : "Requires maker role"}>{busy ? "Calculating…" : "Run margin calculation"}</button>
          <span className="faint">as-of {PINNED_AS_OF} (pinned; no browser clock) · results are immutable and evidence-logged</span>
        </div>
        {err && <div style={{ marginTop: 12 }}><ErrorState error={err} /></div>}
      </form>
      {result && result.market_type === market ? <ResultSummary r={result} /> : results.length === 0 && <div style={{ marginTop: 16 }}><EmptyState title="No result yet for this market">Run a calculation above.</EmptyState></div>}
      {results.length > 0 && (
        <section className="card" style={{ marginTop: 16 }}><h2>Recent results ({market})</h2>
          <div className="scroll-x"><table><thead><tr><th>Result</th><th>Subject</th><th>Status</th><th className="r">Required</th><th>Direction</th></tr></thead><tbody>
            {results.slice(0, 10).map(r => <tr key={r.result_id}>
              <td className="mono"><button style={{ minHeight: 28 }} onClick={async () => setResult(await api<MarginResult>(`/risk/results/${r.result_id}`))}>{r.result_id.slice(0, 8)}</button></td>
              <td>{r.subject_id}</td><td><StatusChip tone={statusTone(r.status)}>{r.status}</StatusChip>{r.hypothetical && " HYPOTHETICAL"}</td>
              <td className="r">{r.required_amount ? <MoneyDisplay {...r.required_amount} signed={false} /> : "—"}</td><td>{r.direction}</td></tr>)}
          </tbody></table></div></section>)}
      <p className="faint" style={{ marginTop: 8 }}>Files on record: {files.length} clearing file(s). This engine never executes trades or transmits instructions.</p>
    </>
  );
}

export function Portfolio({ ctx }: { ctx: RiskCtx }) {
  const { result, market, files } = ctx;
  const etdFiles = files.filter(f => (market === "ETD_IFSC" ? f.venue === "DEMO_IFSC" : f.venue === "DEMO_NSE"));
  const [rec, setRec] = useState<Record<string, unknown> | null>(null);
  const [recErr, setRecErr] = useState<ApiError | null>(null);
  async function reconcile(fid: string) {
    try { setRec(await api("/risk/reconcile", { method: "POST", body: { file_id: fid, snapshot_id: result?.snapshot_id ?? null } })); setRecErr(null); } catch (e) { setRecErr(e as ApiError); }
  }
  if (market !== "OTC") {
    return (
      <>
        <section className="card"><h2>Clearing-style margin files — expected vs imported</h2>
          {etdFiles.length === 0 ? <EmptyState title="No clearing files imported for this profile" /> : (
            <div className="scroll-x"><table><thead><tr><th>File · rev</th><th>Account</th><th className="r">Independent sum of components</th><th className="r">Imported (reported) total</th><th className="r">Difference</th><th>Status</th><th /></tr></thead><tbody>
              {etdFiles.map(f => <tr key={f.file_id + f.revision}>
                <td className="mono">{f.file_id} · r{f.revision}</td><td>{f.account_id}</td>
                <td className="r"><MoneyDisplay {...f.computed_total} signed={false} /></td><td className="r">{f.reported_total ? <MoneyDisplay {...f.reported_total} signed={false} /> : "none"}</td>
                <td className="r">{f.reconciliation.difference ?? "UNKNOWN"}</td>
                <td>{f.status === "RECONCILED" ? <StatusChip tone="ok">RECONCILED</StatusChip> : <StatusChip tone="danger">EXCEPTION</StatusChip>}</td>
                <td><button onClick={() => reconcile(f.file_id)} disabled={!ctx.canWrite}>Re-reconcile</button></td></tr>)}
            </tbody></table></div>)}
          {recErr && <ErrorState error={recErr} />}
          {rec && <pre className="mono card" style={{ overflow: "auto", marginTop: 12 }} aria-label="Reconciliation output">{JSON.stringify(rec, null, 2)}</pre>}
          <p className="faint">Imported margin is a SOURCE OBLIGATION, not an internally computed SPAN figure. Component aggregation is declared per row (ADDITIVE / OFFSET / INFO / REPORTED_TOTAL).</p></section>
        {result?.components.length ? <section className="card" style={{ marginTop: 16 }}><h2>Margin components (latest result)</h2>
          <table><thead><tr><th>Component</th><th className="r">Amount</th></tr></thead><tbody>{result.components.map((c, i) => <tr key={i}><td>{c.kind}{c.hypothetical ? " (HYPOTHETICAL)" : ""}</td><td className="r"><MoneyDisplay amount={c.amount} currency={c.currency} /></td></tr>)}</tbody></table></section> : null}
        {result?.internal_estimate && <section className="card" style={{ marginTop: 16 }}><h2>Internal what-if estimate (separate from the obligation)</h2>
          <StatusChip tone="warn">{result.internal_estimate.label}</StatusChip>
          <p>Estimated worst-case loss at ±{result.internal_estimate.shock_pct} underlying move: <span className="num">{result.internal_estimate.estimated_loss} {result.currency}</span></p>
          <p className="mono faint">{Object.entries(result.internal_estimate.metrics).map(([k, v]) => `${k}=${v}`).join(" · ")}</p></section>}
        <Waterfall r={result} />
      </>
    );
  }
  if (!result || result.market_type !== "OTC") return <EmptyState title="Run an OTC calculation on the Risk desk first" />;
  return (
    <>
      {result.netting && <section className="card"><h2>Netting set {result.netting_set_id}</h2><div className="grid c4">
        <MetricCard label="Gross positive" value={<MoneyDisplay {...result.netting.gross_positive} />} />
        <MetricCard label="Gross negative" value={<MoneyDisplay {...result.netting.gross_negative} />} />
        <MetricCard label="Net" value={<MoneyDisplay {...result.netting.net} />} />
        <MetricCard label="Netting benefit" value={<MoneyDisplay {...result.netting.netting_benefit} signed={false} />} sub="reduction in positive exposure" /></div>
        <p className="faint">{result.netting.netting_benefit_definition}</p></section>}
      <section className="card" style={{ marginTop: 16 }}><h2>Trades (signed PV from our perspective)</h2>
        {result.trade_valuations.length === 0 ? <EmptyState title="No valuations (result blocked)" /> : <div className="scroll-x"><table><thead><tr><th>Trade</th><th>Type · side</th><th>Maturity</th><th className="r">PV (trade ccy)</th><th className="r">FX used</th><th className="r">PV ({result.currency})</th></tr></thead><tbody>
          {result.trade_valuations.map(t => <tr key={t.trade_id}><td className="mono">{t.trade_id}</td><td>{t.type} · {t.side}</td><td>{t.maturity}</td>
            <td className="r"><MoneyDisplay {...t.pv_trade_ccy} /></td><td className="r num">{t.fx_used ?? "—"}</td><td className="r"><MoneyDisplay {...t.pv_base} /></td></tr>)}
        </tbody></table></div>}</section>
      <Waterfall r={result} />
    </>
  );
}

function Waterfall({ r }: { r: MarginResult | null }) {
  if (!r || r.waterfall.length === 0) return null;
  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="wf-h"><h2 id="wf-h">Calculation waterfall (every value traced to an input or term)</h2>
      <div className="scroll-x"><table><thead><tr><th>#</th><th>Step</th><th className="r">Value</th><th>Citation</th></tr></thead><tbody>
        {r.waterfall.map((w, i) => <tr key={i}><td>{i + 1}</td><td>{w.step}</td><td className="r num">{w.value}</td><td className="mono faint">{w.citation}</td></tr>)}
      </tbody></table></div>
      {r.components.length > 0 && <p className="mono faint">components: {r.components.map(c => `${c.kind}=${c.amount} ${c.currency}`).join(" · ")}</p>}
    </section>
  );
}

export function StressLab({ ctx }: { ctx: RiskCtx }) {
  const { results, canWrite, market } = ctx;
  const ok = results.filter(r => r.status === "OK");
  const [base, setBase] = useState(""); const [bp, setBp] = useState(""); const [fxp, setFxp] = useState(""); const [px, setPx] = useState(""); const [vol, setVol] = useState("");
  const [out, setOut] = useState<StressOut | null>(null); const [err, setErr] = useState<ApiError | null>(null); const [busy, setBusy] = useState(false);
  useEffect(() => { setOut(null); setBase(ok[0]?.result_id ?? ""); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [market, results.length]);
  async function go(e: FormEvent) {
    e.preventDefault(); setBusy(true);
    const spec: Record<string, unknown> = {};
    if (bp) spec.curve_shift_bp = bp; if (fxp) spec.fx_shock_pct = { EURUSD: fxp }; if (px) spec.price_shock_pct = px; if (vol) spec.vol_shock_abs = vol;
    try { setOut(await api<StressOut>("/risk/stress", { method: "POST", body: { base_result_id: base, shock_spec: spec } })); setErr(null); }
    catch (x) { setErr(x as ApiError); setOut(null); } finally { setBusy(false); }
  }
  const b = out?.baseline as { exposure?: { amount: string; currency: string } } | undefined, s = out?.scenario as { exposure?: { amount: string; currency: string } } | undefined;
  return (
    <>
      <form className="card" onSubmit={go} aria-label="Stress controls"><h2>Stress lab — deterministic shocks on a copy; baseline is never mutated</h2>
        <div className="grid c4">
          <div><label htmlFor="base">Baseline result (OK only)</label><select id="base" value={base} onChange={e => setBase(e.target.value)}>
            <option value="">— none —</option>{ok.map(r => <option key={r.result_id} value={r.result_id}>{r.result_id.slice(0, 8)} · {r.subject_id}</option>)}</select></div>
          {market === "OTC" ? <>
            <div><label htmlFor="bp">Parallel curve shift (bp)</label><input id="bp" value={bp} onChange={e => setBp(e.target.value)} placeholder="100" inputMode="decimal" /></div>
            <div><label htmlFor="fxp">EURUSD shock (fraction)</label><input id="fxp" value={fxp} onChange={e => setFxp(e.target.value)} placeholder="0.10 = +10%" inputMode="decimal" /></div></> : <>
            <div><label htmlFor="px">Price shock (fraction)</label><input id="px" value={px} onChange={e => setPx(e.target.value)} placeholder="-0.05" inputMode="decimal" /></div>
            <div><label htmlFor="vol">Vol shock (absolute)</label><input id="vol" value={vol} onChange={e => setVol(e.target.value)} placeholder="0.02" inputMode="decimal" /></div></>}
        </div>
        <div className="row" style={{ marginTop: 12 }}><button className="primary" disabled={busy || !base || !canWrite}>{busy ? "Running…" : "Run stress"}</button>
          <span className="faint">Scenario figures are internal what-ifs, not regulatory margin. No unsourced real-price claims.</span></div>
        {err && <div style={{ marginTop: 12 }}><ErrorState error={err} /></div>}
      </form>
      {out && <section className="card" style={{ marginTop: 16 }} aria-labelledby="st-h"><div className="row"><h2 id="st-h" style={{ margin: 0 }}>Baseline vs scenario</h2><StatusChip tone="warn">{out.label}</StatusChip><StatusChip tone="ok">baseline_mutated = {String(out.baseline_mutated)}</StatusChip></div>
        {b?.exposure && s?.exposure && <ExposureBars base={b.exposure} scen={s.exposure} />}
        <div className="scroll-x"><table><caption className="sr-only">Baseline versus scenario</caption><thead><tr><th>Measure</th><th className="r">Baseline</th><th className="r">Scenario</th><th className="r">Δ</th></tr></thead><tbody>
          {Object.entries(out.delta).map(([k, d]) => <tr key={k}><td>{k}</td>
            <td className="r">{k === "exposure" && b?.exposure ? <MoneyDisplay {...b.exposure} /> : "—"}</td>
            <td className="r">{k === "exposure" && s?.exposure ? <MoneyDisplay {...s.exposure} /> : "—"}</td>
            <td className="r"><MoneyDisplay {...d} /></td></tr>)}
        </tbody></table></div>
        {out.dv01 && <p>DV01: <MoneyDisplay {...out.dv01.value} /> <span className="faint">— {out.dv01.method}</span></p>}
        <div className="scroll-x"><table><caption>Breakdown</caption><thead><tr>{Object.keys(out.breakdown[0] ?? {}).map(k => <th key={k}>{k}</th>)}</tr></thead><tbody>
          {out.breakdown.map((row, i) => <tr key={i}>{Object.values(row).map((v, j) => <td key={j} className="num">{typeof v === "object" && v ? ((v as { amount?: string }).amount ?? JSON.stringify(v)) : String(v)}</td>)}</tr>)}
        </tbody></table></div></section>}
    </>
  );
}

/** Accessible chart: SVG with title/desc, labelled axis, plus the table above as the text alternative. Decimal strings -> bar length only. */
function ExposureBars({ base, scen }: { base: { amount: string; currency: string }; scen: { amount: string; currency: string } }) {
  const vb = Number(base.amount), vs = Number(scen.amount); // display geometry only; never used for money arithmetic
  const mx = Math.max(Math.abs(vb), Math.abs(vs), 1);
  const w = (v: number) => Math.round((Math.abs(v) / mx) * 360);
  return (
    <svg role="img" aria-labelledby="bars-t bars-d" width="100%" viewBox="0 0 520 110" style={{ margin: "12px 0" }}>
      <title id="bars-t">Net exposure, baseline vs scenario ({base.currency})</title>
      <desc id="bars-d">Baseline {base.amount}; scenario {scen.amount}. Bar length is absolute value; sign is stated in the label.</desc>
      {[["Baseline", vb, base.amount, 20], ["Scenario", vs, scen.amount, 64]].map(([l, v, a, y]) => (
        <g key={String(l)}><text x="0" y={Number(y) + 16} fill="#a7b9b3" fontSize="12">{String(l)}</text>
          <rect x="80" y={Number(y)} width={w(Number(v))} height="24" fill="#43c8b5" rx="3" />
          <text x={84 + w(Number(v))} y={Number(y) + 16} fill="#e8f1ee" fontSize="12">{Number(v) < 0 ? "−" : "+"}{String(a).replace("-", "")} {base.currency}</text></g>))}
      <line x1="80" y1="10" x2="80" y2="100" stroke="#30413d" />
      <text x="80" y="108" fill="#8a9d97" fontSize="10">0 (axis: |net exposure|, {base.currency})</text>
    </svg>
  );
}

export function MethodologyDrawer({ result }: { result: MarginResult | null }) {
  const [open, setOpen] = useState(false); const [m, setM] = useState<Methodology | null>(null); const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => { if (open && !m) api<Methodology>("/risk/methodology").then(setM).catch(e => setErr(e)); }, [open, m]);
  function exportTrace() {
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `risk-result-${result?.result_id.slice(0, 8)}.json`; a.click(); URL.revokeObjectURL(a.href);
  }
  return (
    <>
      <button onClick={() => setOpen(true)} aria-haspopup="dialog">Methodology</button>
      {open && (
        <aside role="dialog" aria-modal="true" aria-label="Methodology" style={{ position: "fixed", top: 0, right: 0, bottom: 0, width: "min(560px,100vw)", background: "var(--panel)", borderLeft: "1px solid var(--border)", padding: 24, overflow: "auto", zIndex: 30 }}>
          <div className="row"><h2 style={{ margin: 0 }}>Methodology & limitations</h2><span className="spacer" /><button autoFocus onClick={() => setOpen(false)}>Close</button></div>
          {err && <ErrorState error={err} />}
          {m && <>
            <p className="muted">{m.numerics}</p>
            {Object.entries(m.methods).map(([k, v]) => <div key={k}><h3>{k}</h3><p>{v.summary}</p>
              <ul className="mono">{v.formulas.map((f, i) => <li key={i}>{f}</li>)}</ul><h3>Limitations</h3><ul className="muted">{v.limitations.map((f, i) => <li key={i}>{f}</li>)}</ul></div>)}
            <h3>Conventions</h3><ul className="mono">{Object.entries(m.conventions).map(([k, v]) => <li key={k}>{k}: {v}</li>)}</ul></>}
          {result && <><h3>Selected result trace</h3><p className="mono faint">snapshot {result.snapshot_id} · terms {result.terms_hash ?? "n/a"} · result hash {result.result_hash}</p>
            <p className="mono faint">inputs: {result.input_hashes.map(h => h.slice(0, 12)).join(", ")}</p><button onClick={exportTrace}>Export result JSON</button></>}
        </aside>)}
    </>
  );
}
