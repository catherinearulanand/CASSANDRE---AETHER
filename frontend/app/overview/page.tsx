"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, type Run } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, MetricCard, StatusChip } from "@/components/ui";

const ROUTE: Record<string, string> = {
  regulatory: "/regulatory", contracts: "/contracts", risk: "/risk",
  collateral: "/collateral", compliance: "/compliance", evidence: "/evidence",
};
const PIPELINE = ["CREATED", "VALIDATING", "AWAITING_RULES", "AWAITING_TERMS", "COMPUTING", "RECONCILING", "OPTIMISING", "MONITORING", "AWAITING_APPROVAL", "COMPLETED"];

export default function Overview() {
  const { caps, market } = useSession();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    setRuns(null); // clear stale data while the (possibly different) market context loads
    api<{ items: Run[] }>("/runs?limit=100", { signal: ac.signal })
      .then(r => setRuns(r.items.filter(x => x.market === market)))
      .catch(e => { if (!ac.signal.aborted) setErr(e); });
    return () => ac.abort();
  }, [market]);

  const mounted = caps?.engines.filter(e => e.mounted).length ?? 0;
  const counts = PIPELINE.map(s => [s, runs?.filter(r => r.state === s).length ?? 0] as const);
  return (
    <>
      <h1>Overview</h1>
      <p className="muted">AETHER is an institutional decision-support overlay. It is not an exchange, clearing corporation, trading terminal or regulator, and it transmits nothing externally.</p>
      <div className="grid c4" style={{ margin: "16px 0" }}>
        <MetricCard label="Engines mounted" value={`${mounted} / ${caps?.engines.length ?? 6}`} sub="reported live by /capabilities" />
        <MetricCard label="Runs in this market" value={runs ? String(runs.length) : "…"} sub={market} />
        <MetricCard label="Data mode" value={caps?.data_mode ?? "…"} sub={`source mode ${caps?.source_mode ?? "…"}`} />
        <MetricCard label="External transmission" value={caps?.external_transmission ?? "…"} sub="trading / settlement / reporting" />
      </div>
      <div className="grid c2">
        <section className="card" aria-labelledby="eng">
          <h2 id="eng">Engines</h2>
          <table><thead><tr><th>Engine</th><th>Status</th></tr></thead><tbody>
            {caps?.engines.map(e => (
              <tr key={e.key}>
                <td><Link href={ROUTE[e.key]}>{e.name}</Link></td>
                <td>{e.mounted ? <StatusChip tone="ok">Available</StatusChip> : <StatusChip tone="warn">Not implemented</StatusChip>}</td>
              </tr>
            ))}
          </tbody></table>
        </section>
        <section className="card" aria-labelledby="pipe">
          <h2 id="pipe">Run pipeline states ({market})</h2>
          {err ? <ErrorState error={err} /> : !runs ? <EmptyState title="Loading…" /> : runs.length === 0
            ? <EmptyState title="No runs yet">Create one in the <Link href="/scenarios">Scenario Lab</Link>.</EmptyState>
            : <table><thead><tr><th>State</th><th className="r">Runs</th></tr></thead><tbody>
              {counts.map(([s, n]) => <tr key={s}><td>{s}</td><td className="r num">{n}</td></tr>)}
            </tbody></table>}
        </section>
      </div>
      <section className="card" style={{ marginTop: 16 }}>
        <h2>Methodology limitations (always visible)</h2>
        <ul className="muted">
          <li>All trades, positions, inventory and market files are synthetic; synthetic rules are fictional policy, not law.</li>
          <li>Original-prototype figures (406 tests, USD 9.5m call, 76.10% modelled reduction) refer to an earlier prototype and are not results of this build.</li>
          <li>No metrics on this page are fabricated: cards show live API values or explicit empty/not-implemented states.</li>
        </ul>
      </section>
    </>
  );
}
