"use client";
import { useState, type FormEvent } from "react";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import { useSession } from "@/lib/session";
import { API, ConfirmAction, FieldErrors, Severity, fmtTs, useAction, useLoad, type CaseRow } from "./shared";

interface QueueItem extends CaseRow { sla: { due_at: string; overdue: boolean; hours_overdue: number; escalation: string; as_of: string | null } }
interface Waiver { waiver_id: string; control_key: string; subject_id: string; reason: string; status: string; valid_from: string; valid_to: string; requested_by: string; approved_by: string | null; scope_note: string }

export default function OpsQueue({ openCase }: { openCase: (id: string) => void }) {
  const { principal } = useSession();
  const [mine, setMine] = useState(false); const [asOf, setAsOf] = useState("");
  const q = useLoad<{ items: QueueItem[]; as_of: string | null; clock_note: string }>(`${API}/queue?mine=${mine}${asOf ? `&as_of=${encodeURIComponent(asOf)}` : ""}`);
  return (
    <>
      <section className="card" aria-labelledby="oq">
        <div className="row"><h2 id="oq" style={{ margin: 0 }}>Operations queue</h2><span className="spacer" />
          <label className="row" style={{ gap: 6, margin: 0 }}><input type="checkbox" checked={mine} onChange={e => setMine(e.target.checked)} style={{ width: 20, minHeight: 20 }} /> Assigned to me ({principal?.username})</label>
          <label htmlFor="q-as" className="sr-only">SLA as-of</label><input id="q-as" placeholder="SLA as-of (UTC), default latest evaluation" value={asOf} onChange={e => setAsOf(e.target.value)} style={{ width: 280 }} /></div>
        {q.loading ? <EmptyState title="Loading queue…" /> : q.error ? <ErrorState error={q.error} /> : !q.data || q.data.items.length === 0
          ? <EmptyState title="No open cases">An empty queue is not evidence of compliance — check control coverage and UNKNOWN counts.</EmptyState>
          : <>
            <p className="faint">SLA measured at <span className="mono">{fmtTs(q.data.as_of)}</span>. {q.data.clock_note}.</p>
            <div className="scroll-x"><table><caption className="sr-only">Open cases ordered by SLA due time</caption>
              <thead><tr><th>Severity</th><th>Case</th><th>Owner</th><th>State</th><th className="r">Occurrences</th><th>SLA due</th><th>SLA / escalation</th><th>Remediation</th></tr></thead>
              <tbody>{q.data.items.map(c => (
                <tr key={c.case_id}><td><Severity s={c.severity} /></td>
                  <td><button className="btn" style={{ minHeight: 28, textAlign: "left" }} onClick={() => openCase(c.case_id)}>{c.title}</button><div className="mono faint">{c.case_id.slice(0, 8)} · {c.kind}</div></td>
                  <td>{c.owner_id ?? <span className="faint">unassigned</span>}</td><td>{c.state.replace("_", " ")}</td><td className="r num">{c.occurrence_count}</td><td className="mono">{fmtTs(c.sla.due_at)}</td>
                  <td>{c.sla.overdue ? <StatusChip tone="danger">OVERDUE {c.sla.hours_overdue}h · {c.sla.escalation}</StatusChip> : <StatusChip tone="ok">within SLA</StatusChip>}</td>
                  <td>{c.remediations.length ? c.remediations[c.remediations.length - 1].selected.label : <span className="faint">none yet</span>}{c.last_recheck && <div className="faint">recheck {c.last_recheck.status}</div>}</td></tr>))}
              </tbody></table></div></>}
      </section>
      <Waivers />
    </>
  );
}

function Waivers() {
  const { principal, asOf } = useSession();
  const w = useLoad<{ items: Waiver[] }>(`${API}/waivers`);
  const create = useAction(); const decide = useAction();
  const maker = !!principal?.roles.some(r => ["maker", "compliance_officer", "tenant_admin"].includes(r));
  const checker = !!principal?.roles.some(r => ["checker", "compliance_officer"].includes(r));
  const [why, setWhy] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const f = new FormData(e.currentTarget); const g = (k: string) => String(f.get(k) ?? "").trim();
    if (await create.run(`${API}/waivers`, { control_key: g("control_key"), subject_id: g("subject_id"), reason: g("reason"), valid_from: g("valid_from"), valid_to: g("valid_to") })) { w.reload(); (e.target as HTMLFormElement).reset(); }
  }
  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="wv">
      <h2 id="wv">Waivers (internal policy only)</h2>
      <p className="muted">A waiver is narrow (one control, one exact subject), time-limited and approved by an independent checker. It annotates a case; it never changes the raw control outcome and can never override a legal obligation.</p>
      {w.loading ? <EmptyState title="Loading…" /> : w.error ? <ErrorState error={w.error} /> : (w.data?.items.length ?? 0) === 0 ? <EmptyState title="No waivers" /> :
        <div className="scroll-x"><table><caption className="sr-only">Waivers</caption><thead><tr><th>Control</th><th>Subject</th><th>Valid (UTC)</th><th>Status</th><th>Reason</th><th>Decision</th></tr></thead>
          <tbody>{w.data!.items.map(x => (
            <tr key={x.waiver_id}><td className="mono">{x.control_key}</td><td className="mono">{x.subject_id}</td><td className="mono">{fmtTs(x.valid_from)} → {fmtTs(x.valid_to)}</td>
              <td><StatusChip tone={x.status === "ACTIVE" ? "ok" : x.status === "PENDING" ? "warn" : "neutral"}>{x.status}</StatusChip></td><td>{x.reason}<div className="faint">requested by {x.requested_by}{x.approved_by ? ` · decided by ${x.approved_by}` : ""}</div></td>
              <td>{x.status === "PENDING" && checker && x.requested_by !== principal?.user_id ? (
                <><label htmlFor={`wr-${x.waiver_id}`} className="sr-only">Decision reason</label><input id={`wr-${x.waiver_id}`} placeholder="reason" value={why} onChange={e => setWhy(e.target.value)} style={{ width: 160 }} />
                  <ConfirmAction label="Approve" title="Approve waiver" disabled={decide.pending || why.trim().length < 5} onConfirm={async () => { if (await decide.run(`${API}/waivers/${x.waiver_id}/decision`, { decision: "APPROVE", reason: why })) w.reload(); }}>
                    Approving makes this waiver active for exactly {x.control_key} on {x.subject_id} until {fmtTs(x.valid_to)}.</ConfirmAction>
                  <ConfirmAction label="Reject" title="Reject waiver" disabled={decide.pending || why.trim().length < 5} onConfirm={async () => { if (await decide.run(`${API}/waivers/${x.waiver_id}/decision`, { decision: "REJECT", reason: why })) w.reload(); }}>This waiver will not take effect.</ConfirmAction></>
                ) : x.status === "PENDING" ? <span className="faint">awaiting an independent checker</span> : null}</td></tr>))}
          </tbody></table></div>}
      <FieldErrors error={decide.error} />
      {maker && (
        <form onSubmit={submit} className="grid c3" style={{ marginTop: 12 }} aria-label="Request waiver">
          <div><label htmlFor="wv-c">Control key</label><input id="wv-c" name="control_key" required placeholder="CMP-COV-ETD-INR" /></div>
          <div><label htmlFor="wv-s">Exact subject (no wildcards)</label><input id="wv-s" name="subject_id" required placeholder="ACC-DEMO-NSE-001" /></div>
          <div><label htmlFor="wv-r">Reason</label><input id="wv-r" name="reason" required minLength={5} /></div>
          <div><label htmlFor="wv-f">Valid from (UTC)</label><input id="wv-f" name="valid_from" required defaultValue={asOf} /></div>
          <div><label htmlFor="wv-t">Valid to (UTC, ≤ 30 days)</label><input id="wv-t" name="valid_to" required placeholder="2026-08-24T04:00:00Z" /></div>
          <div style={{ alignSelf: "end" }}><button disabled={create.pending}>{create.pending ? "Requesting…" : "Request waiver"}</button></div>
        </form>)}
      <FieldErrors error={create.error} />
    </section>
  );
}
