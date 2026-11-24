"use client";
import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import EngineGate from "@/components/EngineGate";
import { EmptyState } from "@/components/ui";
import CasesView from "@/components/compliance/CasesView";
import ControlCentre from "@/components/compliance/ControlCentre";
import OpsQueue from "@/components/compliance/OpsQueue";
import SurveillanceView from "@/components/compliance/SurveillanceView";
import { Tabs } from "@/components/compliance/shared";

// Owned by A5 SENTINEL. Deep links: /compliance?tab=controls|cases|surveillance|queue&case=<case_id>
const TABS = [["controls", "Control centre"], ["cases", "Case workbench"], ["surveillance", "Surveillance"], ["queue", "Operations queue"]] as const;
type Tab = (typeof TABS)[number][0];

function Inner() {
  const sp = useSearchParams(); const router = useRouter();
  const tab = (TABS.find(t => t[0] === sp.get("tab"))?.[0] ?? "controls") as Tab;
  const caseId = sp.get("case");
  const go = (t: Tab, c: string | null = null) => router.replace(`/compliance?tab=${t}${c ? `&case=${c}` : ""}`);
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>Approved controls are evaluated deterministically; UNKNOWN is never PASS and an empty alert list is not compliance. Statistical anomalies are investigation signals, never findings.</p>
      <Tabs tabs={TABS} value={tab} onChange={t => go(t)} label="Compliance sections" />
      <div role="tabpanel" id={`cmp-panel-${tab}`} aria-labelledby={`cmp-tab-${tab}`}>
        {tab === "controls" && <ControlCentre openCase={id => go("cases", id)} />}
        {tab === "cases" && <CasesView selected={caseId} onSelect={id => go("cases", id)} />}
        {tab === "surveillance" && <SurveillanceView openCase={() => router.replace("/compliance?tab=cases")} />}
        {tab === "queue" && <OpsQueue openCase={id => go("cases", id)} />}
      </div>
    </>
  );
}

export default function Page() {
  return (
    <EngineGate engine="compliance" name="Compliance & Surveillance (SENTINEL)" planned={["Control centre", "Case workbench", "Surveillance workspace", "Operations queue"]}>
      <Suspense fallback={<EmptyState title="Loading…" />}><Inner /></Suspense>
    </EngineGate>
  );
}
