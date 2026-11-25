"use client";
import { ProvenanceBadge, StatusChip } from "@/components/ui";
import { fmt, Money, reasonText, short, StateChip } from "./shared";
import type { Proposal } from "./types";

/** Accessible bar comparison (baseline vs recommended horizon cost) with a text/table alternative alongside. */
function CostCompare({ base, rec, cur, baseName }: { base: string; rec: string; cur: string; baseName: string }) {
  const b = Number(base), r = Number(rec), max = Math.max(b, r, 1e-9); // display scale only; never used for money arithmetic
  const bar = (v: number) => `${Math.max(2, (v / max) * 100)}%`;
  return (
    <figure style={{ margin: 0 }} aria-label="Baseline versus recommended horizon cost">
      <div role="img" aria-label={`${baseName}: ${base} ${cur}. Recommended: ${rec} ${cur}. Horizon cost, not annualised.`}>
        {[[baseName, base, b, "var(--warn)"], ["Recommended", rec, r, "var(--accent)"]].map(([n, v, num, col]) => (
          <div key={String(n)} style={{ display: "grid", gridTemplateColumns: "180px 1fr 160px", alignItems: "center", gap: 8, margin: "6px 0" }}>
            <span className="muted">{String(n)}</span>
            <span style={{ background: "var(--elevated)", borderRadius: 4, height: 14 }}><span style={{ display: "block", width: bar(num as number), height: 14, background: String(col), borderRadius: 4 }} /></span>
            <span className="num">{fmt(String(v))} {cur}</span>
          </div>))}
      </div>
      <figcaption className="faint">Axis: horizon funding cost in {cur} (not annualised). Same values are in the table below.</figcaption>
    </figure>
  );
}

export default function ProposalDetail({ p }: { p: Proposal }) {
  const cur = p.currency; const r = p.result; const b = p.baseline;
  const infeasible = p.state === "INFEASIBLE" || p.solver_status === "infeasible";
  const showSavings = !infeasible && b?.status === "AVAILABLE" && p.objective != null && b.cost != null;
  return (
    <div>
      <div className="row" style={{ marginBottom: 8, flexWrap: "wrap" }}>
        <StateChip state={p.state} />
        <StatusChip tone={p.solver_status === "optimal" ? "ok" : p.solver_status === "feasible" ? "warn" : p.solver_status === "infeasible" ? "danger" : "neutral"}>solver: {p.solver_status}</StatusChip>
        {p.hypothetical && <ProvenanceBadge kind="HYPOTHETICAL" />}
        <ProvenanceBadge kind="SYNTHETIC" />
        <StatusChip tone="info">NOT TRANSMITTED</StatusChip>
        {p.simulated_settlement && <StatusChip tone="warn">{p.simulated_settlement.label}</StatusChip>}
      </div>
      <p className="mono faint">proposal {p.proposal_id} · rev {p.revision} · hash {short(p.proposal_hash)} · obligation {p.obligation_id} · horizon {p.horizon_days} d</p>

      <div className="grid" style={{ margin: "12px 0", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))" }}>
        <div className="card metric" style={{ minWidth: 0, overflowWrap: "anywhere" }}><div className="label">Coverage required</div><div className="value"><Money amount={p.required_coverage} currency={cur} signed={false} /></div></div>
        <div className="card metric" style={{ minWidth: 0, overflowWrap: "anywhere" }}><div className="label">Recommended horizon cost</div><div className="value">{p.objective != null ? <Money amount={p.objective} currency={cur} signed={false} /> : "—"}</div>
          <div className="faint" style={{ fontSize: 12 }}>{p.cost_units}</div></div>
        <div className="card metric" style={{ minWidth: 0, overflowWrap: "anywhere" }}><div className="label">Residual</div><div className="value">{p.residual != null ? <Money amount={p.residual} currency={cur} signed={false} /> : "—"}</div></div>
      </div>

      {infeasible && r?.diagnostics && (
        <div className="card" role="alert" style={{ borderColor: "#6a2f37" }}>
          <StatusChip tone="danger">INFEASIBLE — no allocation, no instruction can be drafted</StatusChip>
          <p>Most coverage obtainable under all legal/policy limits: <strong className="num">{fmt(r.diagnostics.achievable_coverage)} {cur}</strong>; still missing <strong className="num">{fmt(r.diagnostics.remaining)} {cur}</strong>.</p>
          <p className="muted">Binding constraints: {r.diagnostics.binding_constraints?.length ? r.diagnostics.binding_constraints.join("; ") : "none (inventory is the limit)"}. Lots at availability limit: {r.diagnostics.lots_at_availability_limit?.join(", ") || "none"}.</p>
          <p className="faint">{r.diagnostics.note}</p>
        </div>)}
      {p.state === "NOT_REQUIRED" && <div className="card"><StatusChip tone="info">No additional collateral required</StatusChip><p className="muted">Zero call, return or nothing to post: no allocation is produced.</p></div>}

      {showSavings ? (
        <section className="card" style={{ margin: "12px 0" }} aria-labelledby="cmp">
          <h2 id="cmp">Baseline vs recommended</h2>
          <CostCompare base={b!.cost!} rec={p.objective!} cur={cur} baseName={`Baseline ${b!.policy}`} />
          <div className="scroll-x" tabIndex={0} role="region" aria-label="Scrollable table"><table><caption className="sr-only">Cost comparison</caption>
            <thead><tr><th scope="col">Item</th><th scope="col" className="r">Horizon cost ({cur})</th></tr></thead>
            <tbody>
              <tr><td>Baseline {b!.policy}</td><td className="r num">{fmt(b!.cost)}</td></tr>
              <tr><td>Recommended</td><td className="r num">{fmt(p.objective)}</td></tr>
              <tr><td>Difference (baseline − recommended)</td><td className="r num">{fmt(b!.savings_absolute)} {b!.savings_pct != null ? `(${fmt(b!.savings_pct)} %)` : ""}</td></tr>
            </tbody></table></div>
          <p className="faint">{b!.comparability} Modelled horizon cost, not realised savings. A zero or negative difference means no improvement.</p>
        </section>
      ) : !infeasible && p.state !== "NOT_REQUIRED" && (
        <p className="chip neutral" style={{ borderRadius: 8 }}>No savings figure: {b?.status === "UNAVAILABLE" ? "no feasible cash-first baseline exists (" + (b.tried ?? []).map(t => `${t.policy}: ${t.reason}`).join("; ") + ")" : "baseline not computed"}.</p>
      )}

      {(p.legs?.length ?? 0) > 0 && (
        <section className="card" style={{ margin: "12px 0" }} aria-labelledby="legs">
          <h2 id="legs">Allocation legs</h2>
          <div className="scroll-x" tabIndex={0} role="region" aria-label="Scrollable table"><table><caption className="sr-only">Allocation legs with haircut and FX breakdown</caption>
            <thead><tr><th scope="col">Lot</th><th scope="col">Class</th><th scope="col" className="r">Quantity</th><th scope="col" className="r">Gross value</th>
              <th scope="col" className="r">Valuation factor</th><th scope="col" className="r">FX factor</th><th scope="col" className="r">Effective coverage</th>
              <th scope="col" className="r">Horizon cost</th><th scope="col" className="r">Share</th><th scope="col" className="r">Lag (d)</th></tr></thead>
            <tbody>{p.legs!.map(l => (
              <tr key={l.lot_id}><td className="mono">{l.lot_id}</td><td>{l.asset_class} {l.currency}</td><td className="r num">{fmt(l.quantity)}</td>
                <td className="r num">{fmt(l.gross_value)}</td><td className="r num">{l.valuation_factor}</td><td className="r num">{l.fx_factor}</td>
                <td className="r num">{fmt(l.effective_coverage)}</td><td className="r num">{fmt(l.cost_horizon)}</td>
                <td className="r num">{(Number(l.share_of_coverage) * 100).toFixed(2)}%</td><td className="r num">{l.settlement_lag_days}</td></tr>))}</tbody></table></div>
          <p className="faint">Effective coverage = price × FX × valuation factor × FX factor; one discount convention per schedule entry (a 6 % haircut is a 0.94 valuation, never both). Reserved ≠ posted.</p>
        </section>)}

      {(p.constraints?.length ?? 0) > 0 && (
        <section className="card" style={{ margin: "12px 0" }} aria-labelledby="cons">
          <h2 id="cons">Constraints and slack</h2>
          <div className="scroll-x" tabIndex={0} role="region" aria-label="Scrollable table"><table><caption className="sr-only">Constraint slacks</caption>
            <thead><tr><th scope="col">Constraint</th><th scope="col" className="r">Limit</th><th scope="col" className="r">Measured</th><th scope="col" className="r">Slack</th><th scope="col">Binding?</th></tr></thead>
            <tbody>{p.constraints!.map(c => (
              <tr key={c.name}><td>{c.name}</td><td className="r num">{fmt(c.limit)}</td><td className="r num">{fmt(c.measured)}</td><td className="r num">{fmt(c.slack)}</td>
                <td>{c.binding ? <StatusChip tone="warn">binding</StatusChip> : <StatusChip tone="ok">slack</StatusChip>}</td></tr>))}</tbody></table></div>
          {r?.coverage_shadow_price ? <p className="muted">Shadow price of the coverage constraint: <strong className="num">{r.coverage_shadow_price.value}</strong> — {r.coverage_shadow_price.unit}. {r.coverage_shadow_price.note}.</p>
            : <p className="faint">No dual value: duals are reported only for continuous LPs (not for indivisible-lot MILP).</p>}
        </section>)}

      {r && (
        <section className="card" style={{ margin: "12px 0" }} aria-labelledby="solv">
          <h2 id="solv">Solver and independent verification</h2>
          <p className="mono muted">{String((r.solver as Record<string, unknown> | undefined)?.solver ?? "")} {String((r.solver as Record<string, unknown> | undefined)?.highs_version ?? "")} · scipy {String((r.solver as Record<string, unknown> | undefined)?.scipy_version ?? "")} · {r.runtime_s?.toFixed(3)} s · {r.integer_lots ? "MILP (indivisible lots)" : "continuous LP"}{r.mip_gap != null ? ` · gap ${r.mip_gap}` : ""}</p>
          {r.integer_lots && <p className="chip warn" role="note" style={{ borderRadius: 8 }}><span aria-hidden>!</span> Indivisible lots: quantities are whole blocks (mixed-integer model). {p.solver_status === "optimal" ? "The solver proved optimality within its gap tolerance." : "Optimality is NOT claimed (time limit, or a coverage repair was applied) — the allocation was fully re-verified but may cost more than the true optimum."} No shadow price is defined for integer models.</p>}
          {r.rounding && <p className="muted">Rounding to 6 dp: LP objective {fmt(r.rounding.lp_objective)} → rounded {fmt(r.rounding.rounded_cost)} (gap {r.rounding.absolute_gap}). {r.repair_applied ? "A coverage repair was applied, so optimality is not claimed." : ""}</p>}
          {r.verification && (
            <details open={!r.verification.ok}>
              <summary>{r.verification.ok ? <StatusChip tone="ok">Independent verifier: all {r.verification.checks.length} checks pass</StatusChip> : <StatusChip tone="danger">Verifier FAILED — approval blocked</StatusChip>}</summary>
              <ul>{r.verification.checks.map(c => <li key={c.name}>{c.ok ? "✓" : "✕"} {c.name}{c.detail ? ` — ${c.detail}` : ""}</li>)}</ul>
            </details>)}
          {r.expiry_reason && <p className="chip warn" style={{ borderRadius: 8 }}>{r.expiry_reason}</p>}
          {(r.exclusions?.length ?? 0) > 0 && (
            <details><summary>{r.exclusions!.length} lots excluded</summary>
              <ul>{r.exclusions!.map(e => <li key={e.lot_id}><span className="mono">{e.lot_id}</span>: {e.reasons.map(reasonText).join(" · ")}</li>)}</ul></details>)}
          {(r.warnings?.length ?? 0) > 0 && <ul>{r.warnings!.map(w => <li key={w}>{w}</li>)}</ul>}
        </section>)}
    </div>
  );
}
