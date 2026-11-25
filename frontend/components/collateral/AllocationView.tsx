"use client";
import { useState } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState } from "@/components/ui";
import ProposalDetail from "./ProposalDetail";
import { StateChip, useLoad } from "./shared";
import type { Ctx, Proposal } from "./types";

export default function AllocationView({ ctx, selected, onSelect }: { ctx: Ctx; selected: string; onSelect: (id: string) => void }) {
  const { principal } = useSession();
  const canRun = !!principal?.roles.some(r => ["maker", "tenant_admin", "compliance_officer"].includes(r));
  const list = useLoad(s => api<{ items: Proposal[] }>("/collateral/proposals?limit=100", { signal: s }), []);
  const detail = useLoad(s => api<Proposal>(`/collateral/proposals/${selected}`, { signal: s }), [selected], !!selected);
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const ready = !!(ctx.margin && ctx.snap && ctx.deadline);

  async function run() {
    setPending(true); setErr(null);
    try {
      const p = await api<Proposal>("/collateral/optimise", { method: "POST", idempotencyKey: newKey(),
        body: { margin_result_id: ctx.margin, inventory_snapshot_id: ctx.snap, deadline: ctx.deadline.endsWith("Z") ? ctx.deadline : ctx.deadline + "Z", horizon_days: ctx.horizon } });
      list.reload(); onSelect(p.proposal_id);
    } catch (e) { setErr(e as ApiError); } finally { setPending(false); }
  }
  return (
    <div className="grid" style={{ gridTemplateColumns: "minmax(240px, 300px) 1fr" }}>
      <aside className="card" aria-label="Proposals">
        <h2>Proposals</h2>
        <button className="primary" disabled={!ready || !canRun || pending} onClick={run} style={{ width: "100%" }}
          title={!canRun ? "Requires maker role" : !ready ? "Select margin result, snapshot and deadline" : ""}>{pending ? "Optimising…" : "Run optimisation"}</button>
        {err && <div style={{ marginTop: 8 }}><ErrorState error={err} /></div>}
        <div style={{ marginTop: 12 }}>
          {list.loading ? <EmptyState title="Loading…" /> : list.error ? <ErrorState error={list.error} />
            : (list.data?.items.length ?? 0) === 0 ? <EmptyState title="No proposals yet" />
            : <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>{list.data!.items.map(p => (
              <li key={p.proposal_id}>
                <button onClick={() => onSelect(p.proposal_id)} aria-current={selected === p.proposal_id} style={{ width: "100%", textAlign: "left", margin: "4px 0",
                  borderColor: selected === p.proposal_id ? "var(--accent)" : undefined, minHeight: 44 }}>
                  <span className="mono">{p.proposal_id.slice(0, 8)}</span> <StateChip state={p.state} /><br />
                  <span className="faint">{p.kind === "STRESS" ? "STRESS · " : ""}{p.obligation_id} · {p.currency}</span>
                </button></li>))}</ul>}
        </div>
      </aside>
      <div style={{ minWidth: 0 }}>
        {!selected ? <EmptyState title="Select or create a proposal">Optimise to see legs, constraints, slack, exclusions and solver status.</EmptyState>
          : detail.error ? <ErrorState error={detail.error} /> : !detail.data || detail.loading ? <EmptyState title="Loading proposal…" /> : <ProposalDetail p={detail.data} />}
      </div>
    </div>
  );
}
