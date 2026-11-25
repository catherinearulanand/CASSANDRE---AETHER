"use client";
import Link from "next/link";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import RunPicker from "./RunPicker";
import { useLoad } from "./useLoad";
import type { EntityResolution, Lineage, LineageNode } from "./types";

const STAGE_LABEL: Record<string, string> = {
  SOURCE: "Source document", SPAN: "Source span (cited text)", RULE_TERMS: "Approved rule / terms", SNAPSHOT: "Pinned snapshot",
  METHOD: "Methodology version", MARGIN: "Margin result", PROPOSAL: "Allocation proposal", CONTROL: "Control / case", APPROVAL: "Approval", OTHER: "Other",
};

function spanText(n: LineageNode): string | null {
  const s = n.span; if (!s) return null;
  return s.quote ?? s.excerpt ?? null;
}

function Trace({ data, runId }: { data: Lineage; runId: string }) {
  const parents = (id: string) => data.edges.filter(e => e.to === id).map(e => e.from);
  return (
    <>
      <div className="card" aria-live="polite" data-testid="lineage-status">
        {data.complete ? <StatusChip tone="ok">Lineage complete · {data.event_count} events</StatusChip>
          : <StatusChip tone="warn">Lineage INCOMPLETE · {data.missing.length} missing link(s)</StatusChip>}
        {data.missing.length > 0 && (
          <ul style={{ marginBottom: 0 }}>{data.missing.map((m, i) => (
            <li key={i}><strong>{m.kind === "STAGE_ABSENT" ? `Missing stage: ${STAGE_LABEL[m.stage ?? ""] ?? m.stage}` : `Unrecorded parent: ${m.node_id}`}</strong> <span className="muted">— {m.detail}</span></li>))}</ul>)}
      </div>
      <section className="card" aria-labelledby="trace-h"><h2 id="trace-h">Stepwise trace</h2>
        {data.nodes.length === 0 ? <EmptyState title="No evidence recorded for this decision yet" /> : (
          <ol style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 10 }}>
            {data.nodes.map(n => (
              <li key={n.node_id} className="card" style={{ background: "var(--elevated)" }} data-stage={n.stage}>
                <div className="row"><StatusChip tone={n.recorded ? "info" : "danger"}>{`Step ${n.step} · ${STAGE_LABEL[n.stage] ?? n.stage}`}</StatusChip>
                  <span className="mono">{n.entity_type}:{n.entity_id}</span>{!n.recorded && <StatusChip tone="danger">No evidence recorded</StatusChip>}
                  {n.recorded && n.external && <StatusChip tone="neutral">Recorded earlier (outside this decision&apos;s correlation)</StatusChip>}</div>
                {spanText(n) && <blockquote style={{ margin: "8px 0 0", paddingLeft: 12, borderLeft: "2px solid var(--accent)" }}>
                  <span className="faint">Cited source text{n.span?.page !== undefined ? ` (page ${n.span.page})` : ""}:</span> “{spanText(n)}”</blockquote>}
                {n.stage === "SPAN" && !spanText(n) && <p className="faint" style={{ margin: "6px 0 0" }}>Source span id recorded; the quoted text is held by the owning engine and was not copied into the evidence event.</p>}
                {parents(n.node_id).length > 0 && <p className="muted" style={{ margin: "6px 0 0" }}>Derived from: <span className="mono">{parents(n.node_id).join(", ")}</span></p>}
                {n.events.length > 0 && <ul className="mono" style={{ margin: "6px 0 0", paddingLeft: 18 }}>{n.events.map(e => (
                  <li key={e.sequence}><Link href={runId ? `/evidence?run=${runId}&seq=${e.sequence}` : `/evidence?seq=${e.sequence}`}>seq {e.sequence}</Link> · {e.engine} · {e.action} · {e.recorded_at}</li>))}</ul>}
              </li>))}
          </ol>)}
      </section>
    </>
  );
}

export default function LineageView({ runId, setRunId, entityId }: { runId: string; setRunId: (id: string) => void; entityId?: string }) {
  const byEntity = !!entityId && !runId;
  const run = useLoad<Lineage | null>(s => runId ? api<Lineage>(`/evidence/runs/${runId}/lineage`, { signal: s }) : Promise.resolve(null), [runId]);
  const ent = useLoad<EntityResolution | null>(s => byEntity ? api<EntityResolution>(`/evidence/entities/${encodeURIComponent(entityId!)}/lineage`, { signal: s }) : Promise.resolve(null), [entityId, byEntity]);
  const err = byEntity ? ent.error : run.error;
  const loading = byEntity ? ent.loading : run.loading;
  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="card"><RunPicker value={runId} onChange={setRunId} label="Run to trace" />
        <p className="muted" style={{ marginBottom: 0 }}>Reconstructs the source-to-decision chain from recorded evidence. Absent links are shown explicitly, never inferred.</p></div>
      {byEntity && <div className="card" data-testid="entity-banner">Showing the decision(s) that reference <span className="mono">{entityId}</span>.</div>}
      {!runId && !byEntity ? <EmptyState title="Choose a run" /> : err ? <ErrorState error={err} /> : loading ? <EmptyState title="Building lineage…" /> : byEntity ? (
        ent.data && !ent.data.found ? <EmptyState title="No evidence references this id">{ent.data.message}</EmptyState> : ent.data && ent.data.lineage && (
          <>
            {ent.data.decisions.length > 1 && <div className="card"><h2>Decisions referencing this id</h2><ul>
              {ent.data.decisions.map(d => <li key={d.correlation_id} className="mono">{d.match} · {d.run_id ? <Link href={`/evidence?run=${d.run_id}`}>run {d.run_id.slice(0, 8)}</Link> : `correlation ${d.correlation_id.slice(0, 8)} (no run)`}</li>)}</ul></div>}
            <Trace data={ent.data.lineage} runId={ent.data.lineage.run_id ?? ""} />
          </>)
      ) : run.data && <Trace data={run.data} runId={runId} />}
    </div>
  );
}
