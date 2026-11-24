"use client";
import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import EngineGate from "@/components/EngineGate";
import { EmptyState } from "@/components/ui";
import Command from "@/components/regulatory/Command";
import ImpactLab from "@/components/regulatory/ImpactLab";
import RuleDetail from "@/components/regulatory/RuleDetail";
import { Tabs } from "@/components/regulatory/shared";
import Versions from "@/components/regulatory/Versions";
import Workbench from "@/components/regulatory/Workbench";

// Owned by A1 REGULUS. Deep links: /regulatory?tab=command|workbench|versions|impact&rule=<id>&doc=<id>&span=<id>&impact=<id>
const TABS = [["command", "Command centre"], ["workbench", "Source workbench"], ["versions", "Versions & diff"], ["impact", "Impact lab"]] as const;
type Tab = (typeof TABS)[number][0];

function Inner() {
  const sp = useSearchParams(); const router = useRouter();
  const tab = (TABS.find(t => t[0] === sp.get("tab"))?.[0] ?? "command") as Tab;
  const rule = sp.get("rule"), doc = sp.get("doc"), span = sp.get("span"), impact = sp.get("impact");
  const go = (t: Tab, extra: Record<string, string> = {}) => router.replace(`/regulatory?${new URLSearchParams({ tab: t, ...extra })}`);
  const openRule = (id: string) => go(tab === "versions" ? "versions" : "command", { rule: id });
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>Source obligations become reviewed, versioned, executable controls. Extraction only proposes; an independent checker approves exact revisions; activation is an explicit human act. Synthetic policy only — not law.</p>
      <Tabs tabs={TABS} value={tab} onChange={t => go(t)} label="Regulatory sections" />
      <div role="tabpanel" id={`rpanel-${tab}`} aria-labelledby={`rtab-${tab}`}>
        {tab === "command" && <>
          <Command openRule={openRule} openImpact={id => go("impact", { impact: id })} />
          {rule && <div style={{ marginTop: 16 }}><RuleDetail ruleId={rule} onChanged={() => undefined} openSource={(d, s) => go("workbench", { doc: d, span: s })} /></div>}
        </>}
        {tab === "workbench" && <>
          <Workbench key={doc ?? "none"} initialDoc={doc} initialSpan={span} openRule={id => go("command", { rule: id })} />
        </>}
        {tab === "versions" && <>
          <Versions initialRule={rule} openRule={id => go("versions", { rule: id })} />
          {rule && <div style={{ marginTop: 16 }}><RuleDetail ruleId={rule} onChanged={() => undefined} openSource={(d, s) => go("workbench", { doc: d, span: s })} /></div>}
        </>}
        {tab === "impact" && <ImpactLab key={impact ?? "none"} initialImpact={impact} />}
      </div>
    </>
  );
}

export default function Page() {
  return (
    <EngineGate engine="regulatory" name="Regulatory Intelligence (REGULUS)" planned={["Source workbench", "Rule review and approval", "Version timeline and diff", "Impact lab"]}>
      <Suspense fallback={<EmptyState title="Loading…" />}><Inner /></Suspense>
    </EngineGate>
  );
}
