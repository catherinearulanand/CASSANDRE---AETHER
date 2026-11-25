"use client";
import { Suspense, useState, type KeyboardEvent } from "react";
import { useSearchParams } from "next/navigation";
import EngineGate from "@/components/EngineGate";
import { EmptyState } from "@/components/ui";
import AuditExplorer from "@/components/evidence/AuditExplorer";
import ExportsView from "@/components/evidence/ExportsView";
import IntegrityLab from "@/components/evidence/IntegrityLab";
import LineageView from "@/components/evidence/LineageView";

// Owned by A6 VERITAS. Deep links: /evidence?run=<run_id> and /evidence?entity=<entity_id> open the decision lineage.
const TABS = [["audit", "Audit explorer"], ["lineage", "Decision lineage"], ["lab", "Replay & integrity lab"], ["exports", "Evidence exports"]] as const;
type Tab = (typeof TABS)[number][0];

function Inner() {
  const sp = useSearchParams();
  const [runId, setRunId] = useState(sp.get("run") ?? "");
  const entityId = sp.get("entity") ?? "";
  const seq = sp.get("seq") ?? "";
  const wanted = TABS.find(t => t[0] === sp.get("tab"))?.[0];
  const [tab, setTab] = useState<Tab>(wanted ?? (seq ? "audit" : sp.get("run") || sp.get("entity") ? "lineage" : "audit"));
  function onKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const n = (i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length;
    setTab(TABS[n][0]);
    (document.getElementById(`tab-${TABS[n][0]}`) as HTMLButtonElement | null)?.focus();
  }
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>Every consequential action is recorded in a tenant-scoped, hash-chained log. Integrity is proven only against signed checkpoints held outside the database; a local key is not an HSM.</p>
      <div role="tablist" aria-label="Evidence sections" className="row" style={{ borderBottom: "1px solid var(--border)", marginBottom: 16, gap: 4 }}>
        {TABS.map(([k, label], i) => (
          <button key={k} id={`tab-${k}`} role="tab" aria-selected={tab === k} aria-controls={`panel-${k}`} tabIndex={tab === k ? 0 : -1}
            onClick={() => setTab(k)} onKeyDown={e => onKey(e, i)}
            style={{ border: 0, borderBottom: `2px solid ${tab === k ? "var(--accent)" : "transparent"}`, borderRadius: 0, background: "transparent", color: tab === k ? "var(--accent)" : "var(--text-2)", minHeight: 44 }}>{label}</button>))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "audit" && <AuditExplorer initialSeq={seq || undefined} initialCorrelation={sp.get("correlation") ?? undefined} />}
        {tab === "lineage" && <LineageView runId={runId} setRunId={setRunId} entityId={entityId} />}
        {tab === "lab" && <IntegrityLab runId={runId} setRunId={setRunId} />}
        {tab === "exports" && <ExportsView runId={runId} setRunId={setRunId} />}
      </div>
    </>
  );
}

/** Remount when the URL changes so in-app deep links (e.g. seq links from the lineage trace) are honoured. */
function Keyed() { const sp = useSearchParams(); return <Inner key={sp.toString()} />; }

export default function Page() {
  return (
    <EngineGate engine="evidence" name="Evidence & Audit (VERITAS)" planned={["Audit explorer", "Decision lineage", "Replay and integrity lab", "Evidence exports"]}>
      <Suspense fallback={<EmptyState title="Loading…" />}><Keyed /></Suspense>
    </EngineGate>
  );
}
