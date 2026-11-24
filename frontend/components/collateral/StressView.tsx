"use client";
import { useState, type FormEvent } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { EmptyState, ErrorState, MoneyDisplay, ProvenanceBadge, StatusChip } from "@/components/ui";
import ProposalDetail from "./ProposalDetail";
import type { Ctx, StressOut } from "./types";

/** Liquidity stress and substitution lab. Recomputes through the SAME API pipeline; the result is a HYPOTHETICAL record that
 *  can never be approved, reserved or drafted, and no asset is moved. */
export default function StressView({ ctx }: { ctx: Ctx }) {
  const ready = !!(ctx.margin && ctx.snap && ctx.deadline);
  const [form, setForm] = useState({ haircut: "", cost: "", unavailable: "", override: "" });
  const [out, setOut] = useState<StressOut | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [pending, setPending] = useState(false);

  async function run(e: FormEvent) {
    e.preventDefault(); setPending(true); setErr(null); setOut(null);
    const params: Record<string, unknown> = {};
    if (form.haircut) params.haircut_add_pct = form.haircut;
    if (form.cost) params.cost_add_bps = form.cost;
    if (form.override) params.required_coverage_override = form.override;
    if (form.unavailable.trim()) params.unavailable_lot_ids = form.unavailable.split(/[\s,;]+/).filter(Boolean);
    try {
      setOut(await api<StressOut>("/collateral/stress", { method: "POST", idempotencyKey: newKey(), body: {
        margin_result_id: ctx.margin, inventory_snapshot_id: ctx.snap, deadline: ctx.deadline.endsWith("Z") ? ctx.deadline : ctx.deadline + "Z", horizon_days: ctx.horizon, params } }));
    } catch (x) { setErr(x as ApiError); } finally { setPending(false); }
  }
  const field = (k: keyof typeof form, label: string, help: string, id: string) => (
    <div><label htmlFor={id}>{label}</label><input id={id} value={form[k]} onChange={e => setForm({ ...form, [k]: e.target.value })} aria-describedby={`${id}-h`} inputMode="decimal" />
      <div className="faint" id={`${id}-h`} style={{ fontSize: 12 }}>{help}</div></div>);
  return (
    <>
      <div className="chip warn" role="note" style={{ borderRadius: 8, padding: "6px 10px", marginBottom: 12 }}><span aria-hidden>!</span> HYPOTHETICAL — nothing is reserved, approved or moved by a stress run.</div>
      <form className="card" onSubmit={run} aria-label="Stress parameters">
        <div className="grid c4">
          {field("haircut", "Extra haircut on non-cash (percentage points)", "e.g. 5 lowers every non-cash valuation by 5 points", "st-h")}
          {field("cost", "Funding-cost shock (bps p.a.)", "added to each lot's annual cost rate", "st-c")}
          <div><label htmlFor="st-u">Unavailable lots (IDs, comma-separated)</label><input id="st-u" value={form.unavailable} onChange={e => setForm({ ...form, unavailable: e.target.value })} aria-describedby="st-u-h" />
            <div className="faint" id="st-u-h" style={{ fontSize: 12 }}>substitution what-if: lot is removed from the pool</div></div>
          {field("override", "Required-coverage override (what-if)", "validated, stored as hypothetical", "st-o")}
        </div>
        <button className="primary" style={{ marginTop: 12 }} disabled={!ready || pending}>{pending ? "Recomputing…" : "Run stress"}</button>
        {!ready && <span className="muted" style={{ marginLeft: 12 }}>Select a margin result, snapshot and deadline first.</span>}
      </form>
      {err && <div style={{ marginTop: 12 }}><ErrorState error={err} /></div>}
      {!out && !err && !pending && <div style={{ marginTop: 12 }}><EmptyState title="No stress run yet" /></div>}
      {out && (
        <section style={{ marginTop: 16 }} aria-live="polite">
          <div className="row"><ProvenanceBadge kind="HYPOTHETICAL" /><span className="muted">{out.label}</span></div>
          <div className="grid c3" style={{ margin: "12px 0" }}>
            <div className="card metric"><div className="label">Base horizon cost</div><div className="value">{out.base?.objective ? <MoneyDisplay amount={out.base.objective} currency={out.stressed.currency} signed={false} /> : "—"}</div>
              <div className="faint">{out.base ? `solver ${out.base.solver_status}` : "no base allocation needed"}</div></div>
            <div className="card metric"><div className="label">Stressed horizon cost</div><div className="value">{out.stressed.objective ? <MoneyDisplay amount={out.stressed.objective} currency={out.stressed.currency} signed={false} /> : "—"}</div>
              <div className="faint">solver {out.stressed.solver_status}</div></div>
            <div className="card metric"><div className="label">Change in cost</div><div className="value">{out.objective_delta ? <MoneyDisplay amount={out.objective_delta} currency={out.stressed.currency} /> : "—"}</div>
              {out.stressed.state === "INFEASIBLE" && <StatusChip tone="danger">stressed pool is infeasible</StatusChip>}</div>
          </div>
          <details><summary>Stress parameters recorded</summary><pre className="mono">{JSON.stringify(out.stress, null, 2)}</pre></details>
          <ProposalDetail p={out.stressed} />
        </section>)}
    </>
  );
}
