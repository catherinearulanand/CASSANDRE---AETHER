"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, newKey, type Run } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, ProvenanceBadge, StatusChip } from "@/components/ui";

interface Scenario { scenario_id: string; title: string; market: string; as_of: string; seed: number; golden_ref?: string; revision: string }

export default function ScenarioLab() {
  const { principal } = useSession();
  const [sc, setSc] = useState<Scenario[] | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [err, setErr] = useState<ApiError | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const canCreate = !!principal?.roles.some(r => ["maker", "compliance_officer", "tenant_admin"].includes(r));

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([api<{ items: Scenario[] }>("/scenarios"), api<{ items: Run[] }>("/runs?limit=50")]);
      setSc(s.items); setRuns(r.items); setErr(null);
    } catch (e) { setErr(e as ApiError); }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function create(id: string) {
    setPending(id);
    try { await api("/runs", { method: "POST", body: { scenario_id: id }, idempotencyKey: newKey() }); await load(); }
    catch (e) { setErr(e as ApiError); } finally { setPending(null); }
  }
  return (
    <>
      <h1>Scenario Lab</h1>
      <p className="muted">Scenarios pin an as-of time and seed. Creating a run records an evidence event; no hidden precomputed answers are used.</p>
      {err && <ErrorState error={err} />}
      <div className="grid c3" style={{ margin: "16px 0" }}>
        {!sc ? <EmptyState title="Loading scenarios…" /> : sc.length === 0 ? <EmptyState title="No scenarios found" /> : sc.map(s => (
          <article key={s.scenario_id} className="card">
            <div className="row"><StatusChip tone="info">{s.market}</StatusChip><ProvenanceBadge kind="SYNTHETIC" /></div>
            <h2 style={{ marginTop: 8 }}>{s.title}</h2>
            <p className="mono faint">{s.scenario_id} · rev {s.revision} · seed {s.seed}{s.golden_ref ? ` · ${s.golden_ref}` : ""}</p>
            <button className="primary" disabled={!canCreate || pending === s.scenario_id} onClick={() => create(s.scenario_id)}
              title={canCreate ? "" : "Requires maker role"}>{pending === s.scenario_id ? "Creating…" : "Create run"}</button>
          </article>
        ))}
      </div>
      <section className="card"><h2>Runs</h2>
        {runs.length === 0 ? <EmptyState title="No runs yet" /> : (
          <div className="scroll-x"><table><thead><tr><th>Run</th><th>Scenario</th><th>Market</th><th>State</th><th>Mode</th></tr></thead><tbody>
            {runs.map(r => <tr key={r.run_id}><td className="mono"><Link href={`/evidence?run=${r.run_id}`}>{r.run_id.slice(0, 8)}</Link></td>
              <td>{r.scenario_id}</td><td>{r.market}</td><td>{r.state}</td><td>{r.mode}</td></tr>)}
          </tbody></table></div>)}
      </section>
    </>
  );
}
