"use client";
import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, MetricCard, MoneyDisplay, StatusChip } from "@/components/ui";
import { fmt, reasonText, useLoad } from "./shared";
import type { Ctx, Desk } from "./types";

export default function DeskView({ ctx }: { ctx: Ctx }) {
  const ready = !!(ctx.margin && ctx.snap && ctx.deadline);
  const q = new URLSearchParams({ margin_result_id: ctx.margin, inventory_snapshot_id: ctx.snap, deadline: ctx.deadline, horizon_days: String(ctx.horizon) });
  const d = useLoad(s => api<Desk>(`/collateral/desk?${q}`, { signal: s }), [ctx.margin, ctx.snap, ctx.deadline, ctx.horizon], ready);
  const [reason, setReason] = useState("ALL");
  const reasons = useMemo(() => Object.keys(d.data?.exclusions_by_reason ?? {}), [d.data]);
  if (!ready) return <EmptyState title="Select a margin result, inventory snapshot and deadline">The desk is computed from the Risk engine's result and the pinned inventory snapshot; nothing is shown until both exist.</EmptyState>;
  if (d.loading) return <EmptyState title="Loading collateral desk…" />;
  if (d.error) return <ErrorState error={d.error} />;
  const x = d.data!;
  const cur = x.currency;
  const excluded = x.excluded_lots.filter(l => reason === "ALL" || l.reasons.some(r => r.split(":")[0] === reason));
  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <StatusChip tone="info">{x.market_type}</StatusChip><span className="mono muted">obligation {x.obligation_id} · pool currency {cur} · deadline {x.deadline} · horizon {x.horizon_days} d</span>
      </div>
      {x.warnings.map(w => <p key={w} role="note" className="chip warn" style={{ borderRadius: 8, display: "block", margin: "0 0 8px" }}><span aria-hidden>!</span> {w}</p>)}
      <div className="grid c4">
        <MetricCard label="Coverage still to post" value={<MoneyDisplay amount={x.required_coverage_still_needed} currency={cur} signed={false} />} sub="after eligible collateral already posted" />
        <MetricCard label="Eligible coverage by deadline" value={<MoneyDisplay amount={x.eligible_effective_coverage_by_deadline} currency={cur} signed={false} />} sub="effective (post-haircut), not market value" />
        <MetricCard label="Shortfall vs eligible inventory" value={<MoneyDisplay amount={x.shortfall_vs_eligible} currency={cur} signed={false} />}
          sub={x.shortfall_vs_eligible === "0" ? <StatusChip tone="ok">Inventory sufficient (before caps)</StatusChip> : <StatusChip tone="danger">Shortfall</StatusChip>} />
        <MetricCard label="Max coverage under caps" value={x.achievable_under_caps == null ? "—" : <MoneyDisplay amount={x.achievable_under_caps} currency={cur} signed={false} />} sub="concentration caps not relaxed" />
        <MetricCard label="Posted coverage (existing)" value={x.posted_coverage ? <MoneyDisplay amount={x.posted_coverage.eligible_posted.amount} currency={cur} signed={false} /> : "no records"} sub="posted only; proposed/reserved never counts" />
        <MetricCard label="Cash buffer (effective)" value={<MoneyDisplay amount={x.cash_buffer_effective} currency={cur} signed={false} />} sub="eligible CASH lots" />
        <MetricCard label="Active reservations" value={<span className="num">{x.active_reservations.count} · {fmt(x.active_reservations.units)} units</span>} sub="reserved, not posted" />
        <MetricCard label="Late-settling lots" value={<span className="num">{x.late_settling_lots.length}</span>} sub="would settle after the deadline" />
      </div>
      <section className="card" style={{ marginTop: 16 }} aria-labelledby="inv">
        <h2 id="inv">Eligible inventory by class and currency</h2>
        {x.eligible_inventory.length === 0 ? <EmptyState title="No eligible lots for this obligation" /> : (
          <div className="scroll-x" tabIndex={0} role="region" aria-label="Scrollable table"><table><caption className="sr-only">Eligible inventory</caption>
            <thead><tr><th scope="col">Asset class</th><th scope="col">Lot ccy</th><th scope="col" className="r">Lots</th><th scope="col" className="r">Available units</th><th scope="col" className="r">Effective coverage ({cur})</th></tr></thead>
            <tbody>{x.eligible_inventory.map(r => (
              <tr key={r.asset_class + r.currency}><td>{r.asset_class}</td><td>{r.currency}</td><td className="r num">{r.lots}</td>
                <td className="r num">{fmt(r.available_units)}</td><td className="r num">{fmt(r.effective_coverage)}</td></tr>))}</tbody></table></div>)}
      </section>
      <section className="card" style={{ marginTop: 16 }} aria-labelledby="exc">
        <h2 id="exc">Excluded lots and reasons</h2>
        <div className="row" role="group" aria-label="Filter by exclusion reason" style={{ marginBottom: 8 }}>
          <button className={reason === "ALL" ? "primary" : ""} aria-pressed={reason === "ALL"} onClick={() => setReason("ALL")}>All ({x.excluded_lots.length})</button>
          {reasons.map(r => <button key={r} className={reason === r ? "primary" : ""} aria-pressed={reason === r} onClick={() => setReason(r)}>{r.replace(/_/g, " ")} ({x.exclusions_by_reason[r]})</button>)}
        </div>
        {excluded.length === 0 ? <EmptyState title="No lots excluded for this filter" /> : (
          <div className="scroll-x" tabIndex={0} role="region" aria-label="Scrollable table"><table><caption className="sr-only">Excluded lots</caption><thead><tr><th scope="col">Lot</th><th scope="col">Why it cannot be used</th></tr></thead>
            <tbody>{excluded.map(l => <tr key={l.lot_id}><td className="mono">{l.lot_id}</td><td>{l.reasons.map(reasonText).join(" · ")}</td></tr>)}</tbody></table></div>)}
        <p className="faint">Unknown price, rating, FX or entitlement excludes a lot; nothing is defaulted. Wrong-way restrictions use explicit issuer relationships, not name similarity.</p>
      </section>
    </>
  );
}
