"use client";
import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import EngineGate from "@/components/EngineGate";
import { EmptyState } from "@/components/ui";
import AllocationView from "@/components/collateral/AllocationView";
import ApprovalPanel from "@/components/collateral/ApprovalPanel";
import ContextBar from "@/components/collateral/ContextBar";
import DeskView from "@/components/collateral/DeskView";
import StressView from "@/components/collateral/StressView";
import { NotTransmittedBanner, Tabs } from "@/components/collateral/shared";
import type { Ctx } from "@/components/collateral/types";

// Owned by A4 ATLAS. Deep links: /collateral?tab=desk|allocation|stress|approval&margin=<id>&snap=<id>&proposal=<id>&deadline=<iso>&h=<days>
const TABS = [["desk", "Collateral desk"], ["allocation", "Allocation explorer"], ["stress", "Liquidity stress lab"], ["approval", "Approval & draft"]] as const;
type Tab = (typeof TABS)[number][0];

function Inner() {
  const sp = useSearchParams(); const router = useRouter();
  const tab = (TABS.find(t => t[0] === sp.get("tab"))?.[0] ?? "desk") as Tab;
  const ctx: Ctx = { margin: sp.get("margin") ?? "", snap: sp.get("snap") ?? "", deadline: sp.get("deadline") ?? "", horizon: parseInt(sp.get("h") ?? "30", 10) || 30 };
  const proposal = sp.get("proposal") ?? "";
  const go = (t: Tab, c: Ctx = ctx, p: string = proposal) => {
    const q = new URLSearchParams({ tab: t });
    if (c.margin) q.set("margin", c.margin); if (c.snap) q.set("snap", c.snap); if (c.deadline) q.set("deadline", c.deadline);
    q.set("h", String(c.horizon)); if (p) q.set("proposal", p);
    router.replace(`/collateral?${q}`);
  };
  return (
    <>
      <NotTransmittedBanner />
      <ContextBar ctx={ctx} setCtx={c => go(tab, c)} />
      <Tabs tabs={TABS} value={tab} onChange={t => go(t)} label="Collateral sections" />
      <div role="tabpanel" id={`col-panel-${tab}`} aria-labelledby={`col-tab-${tab}`}>
        {tab === "desk" && <DeskView ctx={ctx} />}
        {tab === "allocation" && <AllocationView ctx={ctx} selected={proposal} onSelect={id => go("allocation", ctx, id)} />}
        {tab === "stress" && <StressView ctx={ctx} />}
        {tab === "approval" && <ApprovalPanel proposalId={proposal} />}
      </div>
    </>
  );
}

export default function Page() {
  return (
    <EngineGate engine="collateral" name="Collateral & Liquidity (ATLAS)" planned={["Collateral desk", "Allocation explorer", "Liquidity stress lab", "Approval and draft instruction"]}>
      <Suspense fallback={<EmptyState title="Loading…" />}><Inner /></Suspense>
    </EngineGate>
  );
}
