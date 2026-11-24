"use client";
import Link from "next/link";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import RunPicker from "./RunPicker";
import { useLoad } from "./useLoad";
import type { Lineage } from "./types";

const STAGE_LABEL: Record<string, string> = {
  SOURCE: "Source document", SPAN: "Source span (cited text)", RULE_TERMS: "Approved rule / terms", SNAPSHOT: "Pinned snapshot",
  METHOD: "Methodology version", MARGIN: "Margin result", PROPOSAL: "Allocation proposal", CONTROL: "Control / case", APPROVAL: "Approval", OTHER: "Other",
};

export default function LineageView({ runId, setRunId }: { runId: string; setRunId: (id: string) => void }) {
  const { data, error, loading } = useLoad<Lineage | null>(s => runId ? api<Lineage>(`/evidence/runs/${runId}/lineage`, { signal: s }) : Promise.resolve(null), [runId]);
  const parents = (id: string) => data?.edges.filter(e => e.to === id).map(e => e.from) ?? [];
  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="card"><RunPicker value={runId} onChange={setRunId} label="Run to trace" />
        <p className="muted" style={{ marginBottom: 0 }}>Reconstructs the source-to-decision chain from recorded evidence. Absent links are shown explicitly, never inferred.</p></div>
      {!runId ? <EmptyState title="Choose a run" /> : error ? <ErrorState error={error} /> : loading ? <EmptyState title="Building lineage…" /> : data && (
        <>
          <div className="card" aria-live="polite">
            {data.complete ? <StatusChip tone="ok">Lineage complete · {data.event_count} events</StatusChip>
              : <StatusChip tone="warn">Lineage INCOMPLETE · {data.missing.length} missing link(s)</StatusChip>}
            {data.missing.length > 0 && (
              <ul style={{ marginBottom: 0 }}>{data.missing.map((m, i) => (
                <li key={i}><strong>{m.kind === "STAGE_ABSENT" ? `Missing stage: ${STAGE_LABEL[m.stage ?? ""] ?? m.stage}` : `Unrecorded parent: ${m.node_id}`}</strong> <span className="muted">— {m.detail}</span></li>))}</ul>)}
          </div>
          <section className="card" aria-labelledby="trace-h"><h2 id="trace-h">Stepwise trace</h2>
            {data.nodes.length === 0 ? <EmptyState title="No evidence recorded for this run yet" /> : (
              <ol style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 10 }}>
                {data.nodes.map(n => (
                  <li key={n.node_id} className="card" style={{ background: "var(--elevated)" }}>
                    <div className="row"><StatusChip tone={n.recorded ? "info" : "danger"}>{`Step ${n.step} · ${STAGE_LABEL[n.stage] ?? n.stage}`}</StatusChip>
                      <span className="mono">{n.entity_type}:{n.entity_id}</span>{!n.recorded && <StatusChip tone="danger">No evidence recorded</StatusChip>}</div>
                    {parents(n.node_id).length > 0 && <p className="muted" style={{ margin: "6px 0 0" }}>Derived from: <span className="mono">{parents(n.node_id).join(", ")}</span></p>}
                    {n.events.length > 0 && <ul className="mono" style={{ margin: "6px 0 0", paddingLeft: 18 }}>{n.events.map(e => (
                      <li key={e.sequence}><Link href={`/evidence?run=${runId}&seq=${e.sequence}`}>seq {e.sequence}</Link> · {e.engine} · {e.action} · {e.recorded_at}</li>))}</ul>}
                  </li>))}
              </ol>)}
          </section>
        </>)}
    </div>
  );
}
