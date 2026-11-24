"use client";
import { useState } from "react";
import { api, newKey } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, ProvenanceBadge, StatusChip } from "@/components/ui";
import { fmt, Json, ReasonDialog, Rule, RuleStatus, short, useLoad } from "./shared";

type Action = "SUBMIT" | "APPROVE" | "REJECT" | "ACTIVATE" | "WITHDRAW" | "REVISE";

/** Rule record with citations, policy tests, approval trail and role-aware maker/checker actions. */
export default function RuleDetail({ ruleId, onChanged, openSource }: { ruleId: string; onChanged: () => void; openSource?: (sourceId: string, spanId: string) => void }) {
  const { principal, asOf } = useSession();
  const { data: r, err, loading, reload } = useLoad(() => api<Rule>(`/regulatory/rules/${ruleId}`), [ruleId]);
  const [dlg, setDlg] = useState<Action | null>(null);
  if (loading) return <EmptyState title="Loading rule…" />;
  if (err) return <ErrorState error={err} />;
  if (!r || !principal) return null;
  const me = principal.user_id, roles = principal.roles;
  const isChecker = roles.includes("checker") || roles.includes("compliance_officer");
  const isMaker = roles.includes("maker");
  const own = r.maker_id === me;
  const done = () => { reload(); onChanged(); };

  const actions: { a: Action; label: string; ok: boolean; why?: string }[] = [
    { a: "SUBMIT", label: "Submit for review", ok: r.status === "DRAFT" && isMaker && own, why: r.status !== "DRAFT" ? "Only drafts can be submitted" : !own ? "Only the author submits" : "Maker role required" },
    { a: "APPROVE", label: "Approve exact revision", ok: r.status === "REVIEW_REQUIRED" && isChecker && !own, why: r.status !== "REVIEW_REQUIRED" ? "Not awaiting review" : own ? "You authored this rule: an independent checker must approve" : "Checker role required" },
    { a: "REJECT", label: "Reject (return to draft)", ok: r.status === "REVIEW_REQUIRED" && isChecker && !own, why: own ? "Independent checker required" : "Not awaiting review" },
    { a: "ACTIVATE", label: "Activate / schedule", ok: r.status === "APPROVED" && isChecker, why: r.status !== "APPROVED" ? "Only approved rules can be activated" : "Checker role required" },
    { a: "WITHDRAW", label: "Withdraw (recorded revision)", ok: ["APPROVED", "SCHEDULED", "ACTIVE"].includes(r.status) && isChecker, why: "Only approved/scheduled/active rules, by a checker" },
    { a: "REVISE", label: "New version from this", ok: isMaker, why: "Maker role required" },
  ];
  const facts = (extra: [string, React.ReactNode][] = []): [string, React.ReactNode][] => [
    ["Rule", `${r.rule_key} v${r.version}`], ["Revision bound", String(r.revision)], ["Content hash", r.content_hash], ["Maker", r.maker_id], ["Acting user", `${principal.username} (${roles.join(",")})`], ...extra,
  ];
  async function run(a: Action, reason: string) {
    const base = `/regulatory/rules/${r!.rule_id}`;
    if (a === "SUBMIT" || a === "APPROVE" || a === "REJECT")
      await api(`${base}/review`, { method: "POST", body: { decision: a, reason, expected_revision: r!.revision }, idempotencyKey: newKey() });
    else if (a === "ACTIVATE") await api(`${base}/activate`, { method: "POST", body: { reason, expected_revision: r!.revision, as_of: asOf }, idempotencyKey: newKey() });
    else if (a === "WITHDRAW") await api(`${base}/withdraw`, { method: "POST", body: { reason, expected_revision: r!.revision } });
    else await api(`${base}/revise`, { method: "POST", body: { rationale: reason } });
    done();
  }
  const dlgText: Record<Action, [string, string, [string, React.ReactNode][]]> = {
    SUBMIT: ["Submit for independent review", "Submit", facts([["Effect", "Compile + policy tests + source basis are re-checked; content then awaits an independent checker."]])],
    APPROVE: ["Approve exact revision", "Approve", facts([["Before", r.status], ["After", "APPROVED (content frozen by hash)"], ["Cited spans", String(r.span_ids.length)]])],
    REJECT: ["Reject and return to draft", "Reject", facts([["Before", r.status], ["After", "DRAFT (author may revise)"]])],
    ACTIVATE: ["Activate or schedule", "Activate", facts([["Approved hash", r.approved_hash ?? "—"], ["Effective from", fmt(r.effective_from)], ["Scenario clock", asOf], ["After", new Date(r.effective_from) > new Date(asOf) ? "SCHEDULED (never ACTIVE before its effective time)" : "ACTIVE"]])],
    WITHDRAW: ["Withdraw rule", "Withdraw", facts([["Effect", "Recorded as a new withdrawal at the current recording time; earlier decisions replay unchanged."]])],
    REVISE: ["Create a new version", "Create draft", facts([["Effect", "Clones content as a new DRAFT version (amendment or rollback). History is never rewritten."]])],
  };
  return (
    <div className="card" aria-labelledby="rule-h">
      <div className="row"><h2 id="rule-h" style={{ margin: 0 }}>{r.rule_key} <span className="mono">v{r.version}</span></h2><RuleStatus s={r.effective_state} />
        <ProvenanceBadge kind={r.provenance === "SYNTHETIC" ? "SYNTHETIC" : "PUBLIC_SOURCE"} />
        {r.effective_state !== r.status && <span className="faint">(persisted status {r.status}; effective by clock)</span>}</div>
      <p className="muted" style={{ margin: "6px 0" }}>{r.title}</p>
      <div className="grid c2">
        <table><tbody>
          <tr><th scope="row">Authority</th><td>{r.authority_type} <span className="faint">(fictional policy, not statute)</span></td></tr>
          <tr><th scope="row">Effective</th><td className="mono">{fmt(r.effective_from)} → {r.effective_to ? fmt(r.effective_to) : "open"}</td></tr>
          <tr><th scope="row">Recorded</th><td className="mono">{fmt(r.recorded_at)} · activated {fmt(r.activated_at)}</td></tr>
          <tr><th scope="row">Hash</th><td className="mono" title={r.content_hash}>{short(r.content_hash)} {r.approved_hash && <span className="faint">approved {short(r.approved_hash)}</span>}</td></tr>
          <tr><th scope="row">Maker · revision</th><td className="mono">{r.maker_id} · rev {r.revision}</td></tr>
          <tr><th scope="row">Applicability</th><td>{r.markets.map(m => <StatusChip key={m} tone="info">{m}</StatusChip>)} {r.conflict_group && <StatusChip tone="neutral">group {r.conflict_group}{r.precedence_rank != null ? ` · rank ${r.precedence_rank}` : ""}</StatusChip>}</td></tr>
          <tr><th scope="row">Compile</th><td>{r.compile_report?.ok ? <StatusChip tone="ok">compiles</StatusChip> : <StatusChip tone="danger">does not compile</StatusChip>} <span className="faint">uses {r.compile_report?.dependencies.join(", ") || "—"}</span></td></tr>
        </tbody></table>
        <div>
          <h3>Parameters (value + unit)</h3>
          {Object.keys(r.parameters ?? {}).length === 0 ? <span className="muted">none</span> : <table><tbody>{Object.entries(r.parameters ?? {}).map(([k, p]) => <tr key={k}><th scope="row">{k}</th><td className="num">{p.value} {p.unit}</td></tr>)}</tbody></table>}
          {(r.compile_report?.errors.length ?? 0) > 0 && <div role="alert" className="field-error">{r.compile_report!.errors.map(e => <div key={e.path}>{e.path}: {e.message}</div>)}</div>}
        </div>
      </div>
      <h3 style={{ marginTop: 16 }}>Rationale</h3><p style={{ margin: 0 }}>{r.rationale}</p>
      <h3 style={{ marginTop: 16 }}>Source citations</h3>
      {r.spans?.length ? r.spans.map(s => (
        <blockquote key={s.span_id} style={{ margin: "0 0 8px", padding: "6px 12px", borderLeft: "3px solid var(--accent)", background: "var(--bg)" }}>
          “{s.quote}” <div className="mono faint">page {s.page} · chars {s.start}–{s.end}{s.byte_start != null ? ` · bytes ${s.byte_start}–${s.byte_end}` : ""} · {s.method}
            {openSource && <> · <button style={{ minHeight: 28, padding: "0 8px" }} onClick={() => openSource(s.source_id, s.span_id)}>Open in source viewer</button></>}</div>
        </blockquote>)) : <StatusChip tone="danger">No source span: cannot be reviewed</StatusChip>}
      <div className="grid c2" style={{ marginTop: 12 }}>
        <div><h3>Scope</h3><Json v={r.scope} /></div><div><h3>Condition</h3><Json v={r.condition} /></div>
      </div>
      <h3 style={{ marginTop: 16 }}>Policy tests</h3>
      {r.tests?.length ? <div className="scroll-x"><table><thead><tr><th>Vector</th><th>Expect scope / condition</th><th>Last result</th><th>Result</th></tr></thead><tbody>
        {r.tests.map(t => <tr key={t.test_id}><td>{t.vector.name ?? "vector"}</td><td className="mono">{t.vector.expect_scope ?? "TRUE"} / {t.vector.expect_condition ?? "TRUE"}</td>
          <td className="mono">{t.passed == null ? "not run" : `${(t.last_result as { scope?: string }).scope} / ${(t.last_result as { condition?: string }).condition}`}</td>
          <td>{t.passed == null ? <StatusChip tone="neutral">not run</StatusChip> : t.passed ? <StatusChip tone="ok">pass</StatusChip> : <StatusChip tone="danger">fail</StatusChip>}</td></tr>)}
      </tbody></table></div> : <StatusChip tone="warn">No test vectors</StatusChip>}
      <h3 style={{ marginTop: 16 }}>Approval trail</h3>
      {r.approvals?.length ? <div className="scroll-x"><table><thead><tr><th>Decision</th><th>Maker → checker</th><th>Bound hash</th><th>Rev</th><th>Expires</th></tr></thead><tbody>
        {r.approvals.map(a => <tr key={a.approval_id}><td><StatusChip tone={a.decision === "APPROVED" ? "ok" : a.decision === "PENDING" ? "warn" : "danger"}>{a.decision}</StatusChip></td>
          <td className="mono">{a.maker_id} → {a.checker_id ?? "—"}</td><td className="mono">{short(a.target_hash)}</td><td className="num">{a.target_revision}</td><td className="mono">{fmt(a.expires_at)}</td></tr>)}
      </tbody></table></div> : <span className="muted">No approval recorded yet.</span>}
      {r.review_notes?.map((n, i) => <div key={i} className="muted mono">{fmt(n.at)} · {n.actor} · {n.decision}: {n.reason}</div>)}
      <div className="row" style={{ marginTop: 16 }} role="group" aria-label="Rule actions">
        {actions.map(x => <button key={x.a} disabled={!x.ok} title={x.ok ? "" : x.why} className={x.a === "APPROVE" || x.a === "SUBMIT" ? "primary" : ""} onClick={() => setDlg(x.a)}>{x.label}</button>)}
      </div>
      {actions.filter(x => !x.ok && (x.a === "APPROVE")).map(x => <div key={x.a} className="faint" style={{ marginTop: 6 }}>Approve unavailable: {x.why}</div>)}
      {dlg && <ReasonDialog title={dlgText[dlg][0]} confirmLabel={dlgText[dlg][1]} facts={dlgText[dlg][2]} tone={dlg === "REJECT" || dlg === "WITHDRAW" ? "danger" : "primary"}
        onClose={() => setDlg(null)} onConfirm={reason => run(dlg, reason)} />}
    </div>
  );
}
