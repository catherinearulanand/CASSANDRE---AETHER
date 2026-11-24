"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { EmptyState, ErrorState, ProvenanceBadge, StatusChip } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";
import { API, Amount, ConfirmAction, FieldErrors, Severity, Status, fmtTs, useAction, useLoad, type CaseDetail, type CaseRow } from "./shared";

const NEXT: Record<string, string[]> = {
  OPEN: ["ACKNOWLEDGED", "DISMISSED"], ACKNOWLEDGED: ["INVESTIGATING", "DISMISSED"], INVESTIGATING: ["DISMISSED"],
  REMEDIATION_PROPOSED: ["REVIEW_REQUIRED", "DISMISSED"], REVIEW_REQUIRED: ["RESOLVED", "INVESTIGATING"], RESOLVED: [], DISMISSED: [],
};

export default function CasesView({ selected, onSelect }: { selected: string | null; onSelect: (id: string | null) => void }) {
  const [state, setState] = useState(""); const [severity, setSeverity] = useState(""); const [kind, setKind] = useState("");
  const { market } = useSession();
  const qs = new URLSearchParams({ limit: "100", ...(state && { state }), ...(severity && { severity }), ...(kind && { kind }) }).toString();
  const list = useLoad<{ items: CaseRow[] }>(`${API}/cases?${qs}`);
  return (
    <div className="grid" style={{ gridTemplateColumns: "minmax(280px, 380px) minmax(0, 1fr)", alignItems: "start" }}>
      <section className="card" aria-labelledby="cl" style={{ minWidth: 0 }}>
        <h2 id="cl">Cases</h2>
        <div className="grid c3" style={{ gap: 8, marginBottom: 8 }}>
          <div><label htmlFor="c-st">State</label><select id="c-st" value={state} onChange={e => setState(e.target.value)}><option value="">All</option>
            {["OPEN", "ACKNOWLEDGED", "INVESTIGATING", "REMEDIATION_PROPOSED", "REVIEW_REQUIRED", "RESOLVED", "DISMISSED"].map(s => <option key={s}>{s}</option>)}</select></div>
          <div><label htmlFor="c-sv">Severity</label><select id="c-sv" value={severity} onChange={e => setSeverity(e.target.value)}><option value="">All</option>
            {["CRITICAL", "HIGH", "MEDIUM", "LOW"].map(s => <option key={s}>{s}</option>)}</select></div>
          <div><label htmlFor="c-kd">Kind</label><select id="c-kd" value={kind} onChange={e => setKind(e.target.value)}><option value="">All</option>
            <option value="CONTROL_BREACH">Control breach</option><option value="DATA_GAP">Data gap</option><option value="ANOMALY_INVESTIGATION">Anomaly signal</option></select></div>
        </div>
        {list.loading ? <EmptyState title="Loading cases…" /> : list.error ? <ErrorState error={list.error} /> : !list.data || list.data.items.length === 0
          ? <EmptyState title="No cases match">Deterministic breaches and unknown-input review items appear here after an evaluation; anomaly signals after surveillance runs.</EmptyState>
          : <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {list.data.items.map(c => (
              <li key={c.case_id}>
                <button onClick={() => onSelect(c.case_id)} aria-current={selected === c.case_id ? "true" : undefined}
                  style={{ width: "100%", textAlign: "left", height: "auto", padding: 10, marginBottom: 6, background: selected === c.case_id ? "var(--accent-wash)" : "var(--elevated)" }}>
                  <div className="row" style={{ gap: 6 }}><Severity s={c.severity} /><StatusChip tone="neutral">{c.state.replace("_", " ")}</StatusChip>
                    {c.kind === "ANOMALY_INVESTIGATION" && <StatusChip tone="info">ANOMALY SIGNAL</StatusChip>}</div>
                  <div style={{ marginTop: 4 }}>{c.title}</div>
                  <div className="mono faint">{c.market_type} · due {fmtTs(c.due_at)} · ×{c.occurrence_count}{market !== c.market_type ? "" : ""}</div>
                </button>
              </li>))}
          </ul>}
      </section>
      <div style={{ minWidth: 0 }}>{selected ? <CaseWorkbench id={selected} onChanged={list.reload} /> : <EmptyState title="Select a case to open the investigation workbench" />}</div>
    </div>
  );
}

function CaseWorkbench({ id, onChanged }: { id: string; onChanged: () => void }) {
  const { principal } = useSession();
  const { data: c, error, loading, reload } = useLoad<CaseDetail>(`${API}/cases/${id}`);
  if (loading) return <EmptyState title="Loading case…" />;
  if (error) return <ErrorState error={error} />;
  if (!c) return null;
  const anomaly = c.kind === "ANOMALY_INVESTIGATION";
  const last = c.evaluations[0];
  const refresh = () => { reload(); onChanged(); };
  return (
    <div className="grid" style={{ gap: 16 }}>
      <section className="card" aria-labelledby="cw">
        <div className="row"><h2 id="cw" style={{ margin: 0 }}>{c.title}</h2><span className="spacer" /><Severity s={c.severity} /><StatusChip tone="neutral">{c.state.replace("_", " ")}</StatusChip><ProvenanceBadge kind="SYNTHETIC" /></div>
        <div role="note" className="state" style={{ textAlign: "left", marginTop: 12, borderStyle: "solid", borderColor: anomaly ? "#2f4c68" : "var(--border)" }}>
          {anomaly ? <><StatusChip tone="info">STATISTICAL ANOMALY — INVESTIGATION SIGNAL</StatusChip> <span>{c.anomaly_disclaimer}</span></>
            : <><StatusChip tone={c.last_status === "FAIL" ? "danger" : "warn"}>DETERMINISTIC {c.last_status}</StatusChip> <span>{c.separation_note} Raw outcome is never altered by acknowledgement, waiver or dismissal.</span></>}
        </div>
        <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 16px", marginBottom: 0 }}>
          <dt className="muted">Affected subject</dt><dd className="mono" style={{ margin: 0 }}>{c.subject_id} · {c.market_type}</dd>
          <dt className="muted">Occurrences</dt><dd style={{ margin: 0 }}>{c.occurrence_count} (reopened {c.reopen_count}×) · opened as-of {fmtTs(c.opened_as_of)} · latest {fmtTs(c.last_as_of)}</dd>
          <dt className="muted">Owner / SLA due</dt><dd style={{ margin: 0 }}>{c.owner_id ?? "unassigned"} · due <span className="mono">{fmtTs(c.due_at)}</span></dd>
          {c.waiver_id && <><dt className="muted">Waiver</dt><dd style={{ margin: 0 }}><StatusChip tone="info">WAIVER ACTIVE</StatusChip> {c.waiver_id.slice(0, 8)} — annotation only; outcome unchanged</dd></>}
        </dl>
      </section>

      {!anomaly && last && (
        <section className="card" aria-labelledby="mv">
          <h2 id="mv">Measured value vs limit ({last.is_recheck ? "recheck" : "evaluation"} as-of {fmtTs(last.as_of)})</h2>
          <div className="scroll-x"><table>
            <caption className="sr-only">Latest numeric trace</caption>
            <thead><tr><th>Status</th><th className="r">Measured</th><th className="r">Limit</th><th className="r">Gap</th><th>Rule</th></tr></thead>
            <tbody><tr><td><Status s={last.status} /></td><td className="r"><Amount value={last.measured} unit={last.unit} /></td><td className="r"><Amount value={last.limit} unit={last.unit} /></td>
              <td className="r"><Amount value={last.gap} unit={last.unit} /></td>
              <td className="mono">{last.rule_key} v{last.rule_version}<div className="faint" title={last.rule_hash}>hash {last.rule_hash.slice(0, 12)}…</div></td></tr></tbody></table></div>
          <p className="muted">{last.reasons.join("; ")}</p>
          <details><summary>Rule source and predicate trace</summary>
            <p className="faint">Authority: {c.control?.authority_type} · rule currency: {last.trace.rule_currency ?? "n/a"}</p>
            <pre className="mono" style={{ whiteSpace: "pre-wrap", overflowX: "auto" }}>{JSON.stringify({ scope: c.control?.scope, condition: c.control?.condition, condition_trace: last.trace.condition }, null, 2)}</pre>
            <h3>Input provenance</h3>
            <table><thead><tr><th>Fact</th><th>Value</th><th>Source</th></tr></thead><tbody>
              {Object.entries(last.facts).map(([k, v]) => <tr key={k}><td className="mono">{k}</td><td className="mono">{String(v)}</td><td className="faint">{last.fact_provenance[k]}</td></tr>)}</tbody></table>
          </details>
          <div className="row" style={{ marginTop: 8 }}>
            {c.margin_result_id ? <Link href={`/risk?result=${c.margin_result_id}`}>Margin result {c.margin_result_id}</Link> : <span className="faint">No linked margin result</span>}
            {c.allocation_id ? <Link href={`/collateral?proposal=${c.allocation_id}`}>Proposed action {c.allocation_id}</Link> : <span className="faint">No collateral proposal linked yet</span>}
            <Link href={`/regulatory?rule=${last.rule_key}`}>Rule source {last.rule_key}</Link>
            <Link href={`/evidence?entity=${c.case_id}`}>Evidence trail</Link>
          </div>
        </section>)}

      <Actions c={c} role={principal?.roles ?? []} userId={principal?.user_id ?? ""} done={refresh} />
      <Remediation c={c} />
      <section className="card" aria-labelledby="tl">
        <h2 id="tl">Event timeline (append-only)</h2>
        <ol style={{ paddingLeft: 18, margin: 0 }}>
          {c.events.map(e => (
            <li key={e.id} style={{ marginBottom: 6 }}>
              <span className="mono">{fmtTs(e.as_of ?? e.recorded_at)}</span> · <strong>{e.event_type.replace(/_/g, " ")}</strong>
              {e.from_state && e.to_state ? <> ({e.from_state} → {e.to_state})</> : null} <span className="faint">by {e.actor_id}</span>
              {e.comment && <div className="muted">{e.comment}</div>}
            </li>))}
        </ol>
        <CommentBox id={c.case_id} done={refresh} />
        {c.evaluations.length > 0 && <details style={{ marginTop: 8 }}><summary>{c.evaluations.length} linked evaluation(s) retained</summary>
          <table><thead><tr><th>As-of</th><th>Status</th><th>Gap</th><th>Snapshot</th><th>Kind</th></tr></thead><tbody>
            {c.evaluations.map(e => <tr key={e.evaluation_id}><td className="mono">{fmtTs(e.as_of)}</td><td><Status s={e.status} /></td><td><Amount value={e.gap} unit={e.unit} /></td>
              <td className="mono">{e.snapshot_id ?? "—"}</td><td>{e.is_recheck ? "recheck" : "evaluation"}</td></tr>)}</tbody></table></details>}
      </section>
    </div>
  );
}

function Actions({ c, role, userId, done }: { c: CaseDetail; role: string[]; userId: string; done: () => void }) {
  const [reason, setReason] = useState(""); const [target, setTarget] = useState("");
  const act = useAction();
  const [snap, setSnap] = useState(""); const [asOf, setAsOf] = useState("");
  const re = useAction<{ status: string }>();
  const { asOf: pinned } = useSession();
  useEffect(() => { setReason(""); setTarget(""); act.clear(); re.clear(); setAsOf(pinned); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [c.case_id]);
  const canMake = role.some(r => ["maker", "compliance_officer", "tenant_admin"].includes(r));
  const canCheck = role.some(r => ["checker", "compliance_officer"].includes(r));
  const next = (NEXT[c.state] ?? []).filter(s => s !== "REMEDIATION_PROPOSED");
  async function go(to: string) {
    await act.run(`${API}/cases/${c.case_id}/transition`, { to_state: to, reason: reason || null, expected_revision: c.revision });
    done(); // always reload: on a revision conflict the user sees the current case plus the error
  }
  const pend = c.pending_dismissal;
  return (
    <section className="card" aria-labelledby="ac">
      <h2 id="ac">Review and resolution</h2>
      {pend && <div role="status" className="state" style={{ textAlign: "left", marginBottom: 12 }}><StatusChip tone="warn">DISMISSAL AWAITING INDEPENDENT CHECKER</StatusChip>
        <div>Requested by {pend.requested_by}: “{pend.reason}”. {pend.requested_by === userId ? "You requested this; you cannot approve it." : canCheck ? "You may review and approve against the current case state." : ""}</div></div>}
      {c.resolution && <div role="status" className="state" style={{ textAlign: "left", marginBottom: 12 }}><StatusChip tone="ok">{c.resolution.kind}</StatusChip> {c.resolution.reason} · approved by {c.resolution.approved_by}
        {c.resolution.recheck_evaluation_id && <> · verified by deterministic recheck {c.resolution.recheck_evaluation_id.slice(0, 8)}</>}</div>}
      <div><label htmlFor="rs">Reason (mandatory for dismissal, resolution and sending back)</label><textarea id="rs" rows={2} value={reason} onChange={e => setReason(e.target.value)} style={{ width: "100%", padding: 8 }} /></div>
      <div className="row" style={{ marginTop: 8 }}>
        {next.map(s => (
          <ConfirmAction key={s} label={s === "DISMISSED" ? (pend ? "Approve dismissal" : "Request dismissal") : `Move to ${s.replace("_", " ")}`} title={`Confirm: ${s.replace("_", " ")}`}
            disabled={act.pending || (s === "RESOLVED" ? !canCheck : !canMake && !(s === "DISMISSED" && canCheck))} onConfirm={() => { setTarget(s); void go(s); }}>
            <p>Case <span className="mono">{c.case_id.slice(0, 8)}</span> at revision {c.revision}. Hash-bound: if the case changes first, this action is refused and nothing is applied.</p>
            {s === "RESOLVED" && <p>Resolution requires a PASSING fresh deterministic recheck made after submission for review, and a checker who did not author or submit the remediation.</p>}
            {s === "DISMISSED" && <p>Dismissal needs two people: a maker requests, an independent checker approves. It never changes the control outcome.</p>}
          </ConfirmAction>))}
        {next.length === 0 && <span className="muted">No further transitions: case is {c.state}. A recurrence re-opens it automatically.</span>}
      </div>
      <FieldErrors error={act.error} />
      {target && !act.error && act.pending && <p className="faint">Applying…</p>}
      {c.kind !== "ANOMALY_INVESTIGATION" && !["RESOLVED", "DISMISSED"].includes(c.state) && (
        <div style={{ marginTop: 16 }}>
          <h3>Deterministic recheck (fresh evaluation; the only way a breach can be shown as cleared)</h3>
          <div className="grid c3">
            <div><label htmlFor="rc-s">Snapshot id</label><input id="rc-s" value={snap} onChange={e => setSnap(e.target.value)} /></div>
            <div><label htmlFor="rc-a">As-of (UTC, explicit)</label><input id="rc-a" value={asOf} onChange={e => setAsOf(e.target.value)} /></div>
            <div style={{ alignSelf: "end" }}><button disabled={!canMake && !canCheck || re.pending || !snap} onClick={async () => { if (await re.run(`${API}/cases/${c.case_id}/recheck`, { snapshot_id: snap, as_of: asOf })) done(); }}>{re.pending ? "Rechecking…" : "Run recheck"}</button></div>
          </div>
          <FieldErrors error={re.error} />
          {c.last_recheck && <p>Latest recheck: <Status s={c.last_recheck.status} /> as-of <span className="mono">{fmtTs(c.last_recheck.as_of)}</span>. {c.last_recheck.status !== "PASS" && "Still not clear — a proposal is not posted collateral."}</p>}
        </div>)}
    </section>
  );
}

function Remediation({ c }: { c: CaseDetail }) {
  const [sel, setSel] = useState(""); const [why, setWhy] = useState(""); const [dl, setDl] = useState("");
  const act = useAction(); const [, setTick] = useState(0);
  const { principal } = useSession();
  const can = !!principal?.roles.some(r => ["maker", "compliance_officer", "tenant_admin"].includes(r));
  return (
    <section className="card" aria-labelledby="rm">
      <h2 id="rm">Remediation options and consequences</h2>
      <p className="muted">Options only — none is an executable market action and nothing is transmitted. Selecting one routes a request to the owning engine for its own approval flow.</p>
      {c.state === "INVESTIGATING" && can ? (
        <form onSubmit={async e => { e.preventDefault(); if (await act.run(`${API}/cases/${c.case_id}/remediate`, { expected_revision: c.revision, selected: sel, rationale: why, deadline: dl || null,
          rejected: c.remedy_options.filter(o => o.route !== sel && (document.getElementById(`rej-${o.route}`) as HTMLInputElement | null)?.value).map(o => ({ route: o.route, reason: (document.getElementById(`rej-${o.route}`) as HTMLInputElement).value })) })) setTick(t => t + 1); }}>
          <fieldset style={{ border: 0, padding: 0 }}><legend className="sr-only">Remedy</legend>
            {c.remedy_options.map(o => (
              <div key={o.route} className="card" style={{ marginBottom: 8, background: "var(--elevated)" }}>
                <label style={{ display: "flex", gap: 8, color: "var(--text)" }}><input type="radio" name="remedy" style={{ width: 20, minHeight: 20 }} checked={sel === o.route} onChange={() => setSel(o.route)} />
                  <span><strong>{o.label}</strong>{o.target_engine && <> <StatusChip tone="info">→ {o.target_engine}</StatusChip></>}<br /><span className="muted">Consequence: {o.consequence}</span></span></label>
                {sel && sel !== o.route && <div style={{ marginTop: 6 }}><label htmlFor={`rej-${o.route}`}>Reason for not choosing this option (optional; recorded as rejected)</label><input id={`rej-${o.route}`} /></div>}
              </div>))}
          </fieldset>
          <div className="grid c2"><div><label htmlFor="rm-w">Rationale (mandatory)</label><textarea id="rm-w" rows={2} value={why} onChange={e => setWhy(e.target.value)} style={{ width: "100%", padding: 8 }} /></div>
            <div><label htmlFor="rm-d">Deadline (UTC, defaults to SLA due)</label><input id="rm-d" value={dl} onChange={e => setDl(e.target.value)} /></div></div>
          <button className="primary" disabled={!sel || why.trim().length < 5 || act.pending} style={{ marginTop: 8 }}>{act.pending ? "Recording…" : "Propose remediation"}</button>
          <FieldErrors error={act.error} />
        </form>
      ) : (
        <>
          {c.remediations.length === 0 && <EmptyState title="No remediation proposed yet">{c.state === "INVESTIGATING" ? "Your role cannot propose remediation." : "Move the case to INVESTIGATING to propose one."}</EmptyState>}
          {c.remediations.map(r => (
            <div key={r.id} className="card" style={{ marginBottom: 8, background: "var(--elevated)" }}>
              <div><strong>Selected:</strong> {r.selected.label} {r.selected.target_engine && <StatusChip tone="info">→ {r.selected.target_engine}</StatusChip>}</div>
              <div className="muted">Rationale: {r.rationale} · owner {r.owner_id} · deadline {fmtTs(r.deadline)} · proposed by {r.proposed_by}</div>
              {r.rejected.length > 0 && <div className="muted">Rejected: {r.rejected.map(x => `${x.route} (${x.reason})`).join("; ")}</div>}
            </div>))}
          <details><summary>Options that were presented</summary><ul>{c.remedy_options.map(o => <li key={o.route}><strong>{o.label}</strong> — {o.consequence}</li>)}</ul></details>
        </>)}
    </section>
  );
}

function CommentBox({ id, done }: { id: string; done: () => void }) {
  const [t, setT] = useState(""); const [busy, setBusy] = useState(false); const [err, setErr] = useState<ApiError | null>(null);
  return (
    <form style={{ marginTop: 8 }} onSubmit={async e => { e.preventDefault(); setBusy(true); setErr(null);
      try { await api(`${API}/cases/${id}/comments`, { method: "POST", body: { text: t } }); setT(""); done(); } catch (x) { setErr(x as ApiError); } finally { setBusy(false); } }}>
      <label htmlFor="cm">Add comment (immutable once saved)</label>
      <div className="row"><input id="cm" value={t} onChange={e => setT(e.target.value)} style={{ flex: 1 }} /><button disabled={busy || t.trim().length < 2}>{busy ? "Saving…" : "Comment"}</button></div>
      <FieldErrors error={err} />
    </form>
  );
}
