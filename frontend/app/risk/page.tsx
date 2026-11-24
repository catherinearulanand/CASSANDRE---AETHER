"use client";
import { useState } from "react";
import EngineGate from "@/components/EngineGate";
import { MethodologyDrawer, Portfolio, RiskDesk, StressLab, useRiskContext } from "@/components/risk/RiskPanels";

const TABS = [["desk", "Risk desk"], ["portfolio", "Portfolio & waterfall"], ["stress", "Stress lab"]] as const;

function RiskWorkspace() {
  const ctx = useRiskContext();
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("desk");
  return (
    <>
      <div className="row" role="tablist" aria-label="Risk workspace" style={{ marginBottom: 16 }}>
        {TABS.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "primary" : ""} onClick={() => setTab(k)}>{l}</button>)}
        <span className="spacer" /><MethodologyDrawer result={ctx.result} />
      </div>
      <div role="tabpanel">
        {tab === "desk" && <RiskDesk ctx={ctx} />}
        {tab === "portfolio" && <Portfolio ctx={ctx} />}
        {tab === "stress" && <StressLab ctx={ctx} />}
      </div>
    </>
  );
}

// Owned by A3 VECTOR.
export default function Page() {
  return (
    <EngineGate engine="risk" name="Risk & Margin (VECTOR)" planned={["Risk desk", "Portfolio explorer and waterfall", "Stress lab", "Methodology drawer"]}>
      <RiskWorkspace />
    </EngineGate>
  );
}
