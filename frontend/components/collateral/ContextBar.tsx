"use client";
import { useEffect } from "react";
import { api } from "@/lib/api";
import { ErrorState, ProvenanceBadge } from "@/components/ui";
import { useLoad } from "./shared";
import type { Ctx, Snapshot } from "./types";

/** Obligation context shared by every tab: margin result (from the Risk engine), inventory snapshot, deadline, cost horizon. */
export default function ContextBar({ ctx, setCtx }: { ctx: Ctx; setCtx: (c: Ctx) => void }) {
  const snaps = useLoad(() => api<{ items: Snapshot[] }>("/collateral/inventory"), []);
  const items = snaps.data?.items ?? [];
  const sel = items.find(s => s.snapshot_id === ctx.snap);
  useEffect(() => {
    if (!ctx.snap && items.length) {
      const s = items[0]; const d = new Date(new Date(s.as_of).getTime() + 2 * 86400000).toISOString().slice(0, 16) + "Z";
      setCtx({ ...ctx, snap: s.snapshot_id, deadline: ctx.deadline || d });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items.length]);
  return (
    <form className="card" aria-label="Obligation context" style={{ marginBottom: 16 }} onSubmit={e => e.preventDefault()}>
      <div className="grid c4">
        <div><label htmlFor="ctx-margin">Margin result ID (from Risk &amp; Margin)</label>
          <input id="ctx-margin" className="mono" value={ctx.margin} onChange={e => setCtx({ ...ctx, margin: e.target.value.trim() })} placeholder="result_id" /></div>
        <div><label htmlFor="ctx-snap">Inventory snapshot</label>
          <select id="ctx-snap" value={ctx.snap} onChange={e => setCtx({ ...ctx, snap: e.target.value })}>
            <option value="">{snaps.loading ? "Loading…" : items.length ? "Select…" : "No snapshots imported"}</option>
            {items.map(s => <option key={s.snapshot_id} value={s.snapshot_id}>{s.snapshot_id} · {s.market_type} · {s.currency}</option>)}
          </select></div>
        <div><label htmlFor="ctx-deadline">Settlement deadline (UTC)</label>
          <input id="ctx-deadline" type="datetime-local" value={ctx.deadline.replace("Z", "")} onChange={e => setCtx({ ...ctx, deadline: e.target.value + (e.target.value ? "Z" : "") })} /></div>
        <div><label htmlFor="ctx-h">Cost horizon (days)</label>
          <input id="ctx-h" type="number" min={1} max={3650} value={ctx.horizon} onChange={e => setCtx({ ...ctx, horizon: Math.max(1, parseInt(e.target.value || "1", 10)) })} /></div>
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <ProvenanceBadge kind="SYNTHETIC" />
        {sel && <span className="muted mono">as-of {sel.as_of} · {sel.lot_count} lots · hash {sel.content_hash.slice(0, 12)}… · {sel.has_policy ? "posting policy attached" : "no posting policy (contract schedule only)"}</span>}
      </div>
      {snaps.error && <ErrorState error={snaps.error} />}
    </form>
  );
}
