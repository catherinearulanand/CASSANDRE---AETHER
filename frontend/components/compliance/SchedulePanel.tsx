"use client";
import { useState } from "react";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import { useSession } from "@/lib/session";
import { API, ConfirmAction, FieldErrors, fmtTs, useAction, useLoad } from "./shared";

interface Job { job_id: string; kind: string; status: string; slot: string | null; watermark_as_of: string | null; subjects: number | null; evaluations: number | null; status_counts: Record<string, number> | null; error: string | null }
interface Schedule {
  as_of: string; daily_controls: number; due_slots: string[]; completed_slots: string[]; missed_slots: string[]; watermark_as_of: string | null; stale: boolean;
  rule?: string; note: string; jobs: Job[];
}

/** S01: scheduled evaluation on an EXPLICIT business clock. Never the browser/server clock: the as-of is typed or the pinned scenario as-of. */
export default function SchedulePanel({ canRun, onDone }: { canRun: boolean; onDone: () => void }) {
  const { asOf } = useSession();
  const [clock, setClock] = useState(asOf);
  const { data, error, loading, reload } = useLoad<Schedule>(`${API}/schedule?as_of=${encodeURIComponent(clock)}`);
  const sweep = useAction<{ slot: string; status: string; evaluations: number; subjects: number; status_counts: Record<string, number> }>();
  const check = useAction<{ newly_recorded_missed: string[] }>();

  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="sch">
      <div className="row"><h2 id="sch" style={{ margin: 0 }}>Scheduled evaluation (daily controls)</h2><span className="spacer" />
        <div style={{ minWidth: 260 }}>
          <label htmlFor="sch-asof">Business as-of (UTC) — explicit clock</label>
          <input id="sch-asof" className="mono" value={clock} onChange={e => setClock(e.target.value)} aria-describedby="sch-help" />
        </div>
      </div>
      <p id="sch-help" className="faint">A slot is due at 12:00Z + 2h grace on its business date (fictional close, not an exchange schedule). A sweep re-reads inputs from the owning engines; stale inputs evaluate to UNKNOWN, never PASS.</p>
      {loading && <EmptyState title="Loading schedule…" />}
      {error && <ErrorState error={error} />}
      {data && (
        <>
          <div className="row" role="status" aria-live="polite" style={{ gap: 12 }}>
            {data.daily_controls === 0 ? <StatusChip tone="warn">NO DAILY CONTROLS REGISTERED</StatusChip>
              : data.missed_slots.length > 0 ? <StatusChip tone="danger">{data.missed_slots.length} MISSED SCHEDULED RUN(S): {data.missed_slots.join(", ")}</StatusChip>
              : data.watermark_as_of ? <StatusChip tone="ok">NO MISSED RUNS FOR {data.due_slots.length} DUE SLOT(S)</StatusChip>
              : <StatusChip tone="warn">NO SWEEP HAS EVER RUN — controls were not evaluated on a schedule</StatusChip>}
            {data.stale && data.daily_controls > 0 && <StatusChip tone="warn">WATERMARK STALE</StatusChip>}
            <span className="mono">watermark {data.watermark_as_of ? fmtTs(data.watermark_as_of) : "none"}</span>
            <span className="faint">{data.completed_slots.length} completed slot(s)</span>
          </div>
          <p className="faint">{data.note}</p>
          <div className="row">
            <ConfirmAction label={sweep.pending ? "Running…" : "Run scheduled sweep"} title="Run scheduled sweep?" disabled={!canRun || sweep.pending} primary
              onConfirm={async () => { if (await sweep.run(`${API}/schedule/run`, { as_of: clock })) { reload(); onDone(); } }}>
              Evaluates every DAILY control for every known subject as of <span className="mono">{clock}</span>. Evaluations and cases are internal records; nothing is transmitted.
            </ConfirmAction>
            <button disabled={!canRun || check.pending} onClick={async () => { if (await check.run(`${API}/schedule/check`, { as_of: clock })) reload(); }}>
              {check.pending ? "Checking…" : "Detect & record missed runs"}</button>
            {!canRun && <span className="faint">Maker, compliance officer or admin role required.</span>}
          </div>
          <FieldErrors error={sweep.error} /><FieldErrors error={check.error} />
          {sweep.result && <p role="status">Sweep {sweep.result.status}: slot {sweep.result.slot}, {sweep.result.subjects} subject(s), {sweep.result.evaluations} evaluation(s) — {Object.entries(sweep.result.status_counts).map(([k, v]) => `${k} ${v}`).join(", ") || "none"}.</p>}
          {check.result && <p role="status">{check.result.newly_recorded_missed.length} newly missed slot(s) recorded as evidence.</p>}
          {data.jobs.length > 0 && (
            <div className="scroll-x"><table>
              <caption className="sr-only">Recent scheduled jobs</caption>
              <thead><tr><th>Slot</th><th>Kind</th><th>Status</th><th>Watermark</th><th className="r">Subjects</th><th className="r">Evaluations</th></tr></thead>
              <tbody>{data.jobs.map(j => (
                <tr key={j.job_id}><td className="mono">{j.slot}</td><td>{j.kind}</td>
                  <td><StatusChip tone={j.status === "DONE" ? "ok" : j.status === "MISSED" || j.status === "FAILED" ? "danger" : "neutral"}>{j.status}</StatusChip>{j.error && <div className="faint">{j.error}</div>}</td>
                  <td className="mono">{fmtTs(j.watermark_as_of)}</td><td className="r num">{j.subjects ?? "—"}</td><td className="r num">{j.evaluations ?? "—"}</td></tr>))}
              </tbody></table></div>)}
        </>
      )}
    </section>
  );
}
