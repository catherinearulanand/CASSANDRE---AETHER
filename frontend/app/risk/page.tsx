"use client";
import { useRef, useState, type KeyboardEvent } from "react";
import EngineGate from "@/components/EngineGate";
import { MethodologyDrawer, Portfolio, RiskDesk, StressLab, useRiskContext } from "@/components/risk/RiskPanels";

const TABS = [["desk", "Risk desk"], ["portfolio", "Portfolio & waterfall"], ["stress", "Stress lab"]] as const;
type Tab = (typeof TABS)[number][0];

function RiskWorkspace() {
  const ctx = useRiskContext();
  const [tab, setTab] = useState<Tab>("desk");
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  // WAI-ARIA tabs: roving tabindex; Left/Right wrap, Home/End jump; focus follows the selection.
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = TABS.findIndex(t => t[0] === tab);
    const n = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (n < 0) return;
    e.preventDefault(); setTab(TABS[n][0]); refs.current[TABS[n][0]]?.focus();
  }
  return (
    <>
      <div className="row" style={{ marginBottom: 16 }}>
        <div role="tablist" aria-label="Risk workspace" className="row" onKeyDown={onKey}>
          {TABS.map(([k, l]) => (
            <button key={k} ref={el => { refs.current[k] = el; }} role="tab" id={`risk-tab-${k}`} aria-controls={`risk-panel-${k}`} aria-selected={tab === k}
              tabIndex={tab === k ? 0 : -1} className={tab === k ? "primary" : ""} onClick={() => setTab(k)}>{l}</button>))}
        </div>
        <span className="spacer" /><MethodologyDrawer result={ctx.result} />
      </div>
      <div role="tabpanel" id={`risk-panel-${tab}`} aria-labelledby={`risk-tab-${tab}`} tabIndex={0}>
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
