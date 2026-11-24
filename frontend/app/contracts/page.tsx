"use client";
import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import EngineGate from "@/components/EngineGate";
import { api, ApiError } from "@/lib/api";
import { EmptyState, ErrorState, ProvenanceBadge, StatusChip } from "@/components/ui";
import Registry from "@/components/contracts/Registry";
import ReviewWorkspace from "@/components/contracts/ReviewWorkspace";
import FreezeTimeline from "@/components/contracts/FreezeTimeline";
import EligibilityNetting from "@/components/contracts/EligibilityNetting";
import type { PkgDetail, TermsView } from "@/components/contracts/types";

// Owned by A2 COVENANT. Deep link: /contracts?pkg=<package_id>&tab=review|freeze|eligibility
const TABS = [["review", "Document review"], ["freeze", "Freeze & amendments"], ["eligibility", "Eligibility & netting"]] as const;
type Tab = (typeof TABS)[number][0];

function Inner() {
  const sp = useSearchParams(); const router = useRouter();
  const pkgId = sp.get("pkg"); const tab = (sp.get("tab") as Tab) || "review";
  const [pkg, setPkg] = useState<PkgDetail | null>(null); const [terms, setTerms] = useState<TermsView | null>(null);
  const [err, setErr] = useState<ApiError | null>(null); const [tick, setTick] = useState(0);

  const load = useCallback(async () => {
    if (!pkgId) return;
    try {
      const [p, t] = await Promise.all([api<PkgDetail>(`/contracts/${pkgId}`), api<TermsView>(`/contracts/${pkgId}/terms`)]);
      setPkg(p); setTerms(t); setErr(null);
    } catch (e) { setErr(e as ApiError); }
    setTick(x => x + 1);
  }, [pkgId]);
  useEffect(() => { setPkg(null); setTerms(null); setErr(null); load(); }, [load]); // clear stale data when the selection changes

  const nav = (p: string | null, t: Tab) => router.replace(p ? `/contracts?pkg=${p}&tab=${t}` : "/contracts");
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>Bilateral OTC agreement terms only — never exchange-traded contract specifications. Document text is untrusted data; every material term is reviewed by an independent checker before it can be frozen.</p>
      <Registry selected={pkgId} onSelect={id => nav(id, "review")} refreshKey={tick} />
      {!pkgId ? <div style={{ marginTop: 16 }}><EmptyState title="Select an agreement above to review its documents and terms" /></div>
        : err ? <div style={{ marginTop: 16 }}><ErrorState error={err} /></div>
        : !pkg || !terms ? <div style={{ marginTop: 16 }}><EmptyState title="Loading agreement…" /></div> : (
          <div style={{ marginTop: 16 }}>
            <div className="row" style={{ marginBottom: 8 }}>
              <h2 style={{ margin: 0 }}>{pkg.netting_set_id}</h2><StatusChip tone={pkg.status === "FROZEN" ? "ok" : "warn"}>{pkg.status.replace(/_/g, " ")}</StatusChip>
              <ProvenanceBadge kind="SYNTHETIC" /><span className="muted">{pkg.party_a_name} (A) · {pkg.party_b_name} (B) · base {pkg.base_currency} · {pkg.governing_law}</span>
            </div>
            <div role="tablist" aria-label="Agreement sections" className="row" style={{ borderBottom: "1px solid var(--border)", marginBottom: 16, gap: 4 }}>
              {TABS.map(([k, label]) => <button key={k} role="tab" aria-selected={tab === k} onClick={() => nav(pkgId, k)}
                style={{ border: 0, borderBottom: `2px solid ${tab === k ? "var(--accent)" : "transparent"}`, borderRadius: 0, background: "transparent", color: tab === k ? "var(--accent)" : "var(--text-2)", minHeight: 44 }}>{label}</button>)}
            </div>
            <div role="tabpanel">
              {tab === "review" && <ReviewWorkspace pkg={pkg} terms={terms} reload={load} />}
              {tab === "freeze" && <FreezeTimeline pkg={pkg} terms={terms} reload={load} />}
              {tab === "eligibility" && <EligibilityNetting pkg={pkg} terms={terms} />}
            </div>
          </div>)}
    </>
  );
}

export default function Page() {
  return (
    <EngineGate engine="contracts" name="Contract Intelligence (COVENANT)" planned={["Agreement registry", "Document review", "Terms freeze and amendments", "Eligibility and netting map"]}>
      <Suspense fallback={<EmptyState title="Loading…" />}><Inner /></Suspense>
    </EngineGate>
  );
}
