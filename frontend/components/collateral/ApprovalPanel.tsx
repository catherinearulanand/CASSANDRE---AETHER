"use client";
import Link from "next/link";
import { useState } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, MoneyDisplay, StatusChip } from "@/components/ui";
import { ConfirmDialog, fmt, short, StateChip, useLoad } from "./shared";
import type { Instruction, Proposal } from "./types";

type Act = "approve" | "reserve" | "cancel" | "draft" | "settle" | null;

export default function ApprovalPanel({ proposalId }: { proposalId: string }) {
  const { principal } = useSession();
  const roles = principal?.roles ?? [];
  const isChecker = roles.some(r => ["checker", "compliance_officer"].includes(r));
  const isMaker = roles.some(r => ["maker", "tenant_admin", "compliance_officer"].includes(r));
  const prop = useLoad(s => api<Proposal>(`/collateral/proposals/${proposalId}`, { signal: s }), [proposalId], !!proposalId);
  const p = prop.data;
  const draftable = p && ["DRAFTED"].includes(p.state);
  const instr = useLoad(s => api<Instruction>(`/collateral/proposals/${proposalId}/instruction`, { signal: s }), [proposalId, p?.state], !!draftable);
  const [act, setAct] = useState<Act>(null);
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);

  if (!proposalId) return <EmptyState title="Select a proposal in the Allocation explorer first" />;
  if (prop.loading) return <EmptyState title="Loading proposal…" />;
  if (prop.error) return <ErrorState error={prop.error} />;
  if (!p) return null;
  const cur = p.currency;
  const base = `/collateral/proposals/${p.proposal_id}`;

  async function exec(reason: string) {
    if (!p || !act) return;
    setPending(true); setErr(null);
    try {
      if (act === "approve") await api(`${base}/approve`, { method: "POST", idempotencyKey: newKey(), body: { reason, expected_revision: p.revision } });
      if (act === "reserve") await api(`${base}/reserve`, { method: "POST", idempotencyKey: newKey(), body: { approval_id: p.approval_id, expected_revision: p.revision } });
      if (act === "cancel") await api(`${base}/cancel`, { method: "POST", idempotencyKey: newKey(), body: { reason, expected_revision: p.revision } });
      if (act === "draft") await api(`${base}/draft`, { method: "POST", idempotencyKey: newKey(), body: { expected_revision: p.revision } });
      if (act === "settle") await api(`${base}/simulate-settlement`, { method: "POST", idempotencyKey: newKey(), body: { expected_revision: p.revision } });
      setAct(null); prop.reload();
    } catch (e) { setErr(e as ApiError); if ((e as ApiError).status === 409) prop.reload(); } // optimistic-lock conflict: reload latest revision
    finally { setPending(false); }
  }
  async function reverify() {
    setPending(true); setErr(null);
    try { await api(`${base}/verify`, { method: "POST" }); prop.reload(); } catch (e) { setErr(e as ApiError); } finally { setPending(false); }
  }
  function download() {
    if (!instr.data) return;
    const blob = new Blob([JSON.stringify(instr.data, null, 2)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `instruction-draft-${p!.proposal_id.slice(0, 8)}-NOT_TRANSMITTED.json`; a.click(); URL.revokeObjectURL(a.href);
  }
  const selfMaker = principal?.user_id === p.maker_id;
  const legs = p.legs ?? [];
  const confirmBody = (
    <div>
      <p className="muted">You are acting as <strong>{principal?.username}</strong> ({roles.join(", ")}). Proposal made by <span className="mono">{p.maker_id}</span>.</p>
      <div className="scroll-x"><table><caption className="sr-only">Before and after</caption>
        <thead><tr><th scope="col">Item</th><th scope="col" className="r">Before</th><th scope="col" className="r">After</th></tr></thead>
        <tbody>
          <tr><td>Proposal state</td><td className="r">{p.state}</td><td className="r">{act === "approve" ? "APPROVED" : act === "reserve" ? "RESERVED" : act === "cancel" ? "CANCELLED" : act === "draft" ? "DRAFTED" : "SIMULATED SETTLED"}</td></tr>
          <tr><td>Reserved lot units</td><td className="r num">{act === "cancel" ? legs.map(l => `${l.lot_id}: ${fmt(l.quantity)}`).join("; ") || "—" : "—"}</td>
            <td className="r num">{act === "reserve" ? legs.map(l => `${l.lot_id}: ${fmt(l.quantity)}`).join("; ") : act === "cancel" ? "released" : "—"}</td></tr>
          <tr><td>Coverage</td><td className="r num" colSpan={2}>{fmt(p.result?.coverage)} {cur} vs required {fmt(p.required_coverage)} {cur}</td></tr>
        </tbody></table></div>
      <p className="mono faint">Binds proposal hash {p.proposal_hash} · revision {p.revision} · margin {short(p.margin_result_hash)} · inventory {short(p.inventory_hash)} · terms {short(p.terms_hash)} · rules {short(p.rule_set_hash)}</p>
      <p>Consequence: {act === "approve" ? "records an independent approval bound to exactly these hashes; it expires and is void if any input changes. It does not reserve or post anything."
        : act === "reserve" ? "atomically reserves the listed units (reserved ≠ posted). A concurrent reservation of the same units will make this fail rather than double-pledge."
        : act === "cancel" ? "releases any reservation; history is retained."
        : act === "draft" ? "creates an internal instruction DRAFT marked NOT_TRANSMITTED. Nothing is sent."
        : "records a SIMULATED confirmed movement for the demo only; no real movement occurs."}</p>
      {err && <ErrorState error={err} />}
    </div>);

  return (
    <>
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Proposal <span className="mono">{p.proposal_id.slice(0, 8)}</span></h2><StateChip state={p.state} />
        {p.hypothetical && <StatusChip tone="warn">HYPOTHETICAL — not approvable</StatusChip>}
        <span className="spacer" />
        <Link href="/evidence">Audit trail in Evidence ↗</Link>
      </div>
      <p className="mono faint">correlation {p.correlation_id} · expires {p.expires_at} · hash {short(p.proposal_hash)} · revision {p.revision}</p>
      <div className="grid c2">
        <section className="card" aria-labelledby="ap">
          <h2 id="ap">Maker–checker controls</h2>
          <p className="muted">Maker: <span className="mono">{p.maker_id}</span>{selfMaker ? " (you)" : ""}. An independent checker must approve the exact revision and hash; approvals expire and become void if inventory, margin or terms change.</p>
          <div className="row">
            <button disabled={pending} onClick={reverify}>Re-verify</button>
            <button className="primary" disabled={!isChecker || p.state !== "CHECKED" || selfMaker || p.kind !== "PROPOSAL"} onClick={() => { setErr(null); setAct("approve"); }}
              title={selfMaker ? "You made this proposal; an independent checker is required" : !isChecker ? "Requires checker role" : p.state !== "CHECKED" ? "Only CHECKED proposals can be approved" : ""}>Approve…</button>
            <button disabled={!(isMaker || isChecker) || p.state !== "APPROVED"} onClick={() => { setErr(null); setAct("reserve"); }}>Reserve…</button>
            <button disabled={!(isMaker || isChecker) || p.state !== "RESERVED"} onClick={() => { setErr(null); setAct("draft"); }}>Draft instruction…</button>
            <button disabled={!(isMaker || isChecker) || !["PROPOSED", "CHECKED", "APPROVED", "RESERVED", "DRAFTED"].includes(p.state) || !!p.simulated_settlement}
              onClick={() => { setErr(null); setAct("cancel"); }}>Cancel…</button>
          </div>
          {selfMaker && p.state === "CHECKED" && <p className="chip warn" style={{ borderRadius: 8 }}><span aria-hidden>!</span> You created this proposal — a different checker must approve it.</p>}
          {p.state === "EXPIRED" && <p className="chip danger" role="alert" style={{ borderRadius: 8 }}><span aria-hidden>✕</span> Expired or invalidated ({p.result?.expiry_reason ?? "time limit"}); the approval is void. Run a new optimisation.</p>}
          {p.state === "INFEASIBLE" && <p className="chip danger" style={{ borderRadius: 8 }}>Infeasible: no approval or instruction is possible.</p>}
          {err && !act && <ErrorState error={err} />}
          <table style={{ marginTop: 12 }}><caption className="sr-only">Bound hashes</caption><tbody>
            <tr><th scope="row">Proposal hash</th><td className="mono">{p.proposal_hash}</td></tr>
            <tr><th scope="row">Margin result</th><td className="mono">{p.margin_result_id} · {short(p.margin_result_hash)}</td></tr>
            <tr><th scope="row">Inventory</th><td className="mono">{p.inventory_snapshot_id} · {short(p.inventory_hash)}</td></tr>
            <tr><th scope="row">Terms / rules</th><td className="mono">{short(p.terms_hash)} / {short(p.rule_set_hash)}</td></tr>
            <tr><th scope="row">Approval</th><td className="mono">{p.approval_id ?? "none"}</td></tr>
          </tbody></table>
        </section>
        <section className="card" aria-labelledby="ins">
          <h2 id="ins">Draft instruction</h2>
          <div role="note" className="chip warn" style={{ borderRadius: 8, padding: "6px 10px" }}><span aria-hidden>!</span> NOT TRANSMITTED — internal draft; no custodian, CCP or settlement system is contacted.</div>
          {p.state !== "DRAFTED" ? <div style={{ marginTop: 12 }}><EmptyState title="No instruction draft">Created only from a RESERVED, feasible proposal. {p.state === "INFEASIBLE" ? "This proposal is infeasible." : ""}</EmptyState></div>
            : instr.loading ? <EmptyState title="Loading draft…" /> : instr.error ? <ErrorState error={instr.error} />
            : instr.data && (
              <div>
                <p className="mono">{instr.data.content.watermark}</p>
                <p className="mono faint">content hash {instr.data.content_hash}</p>
                <div className="scroll-x"><table><caption className="sr-only">Draft legs</caption><thead><tr><th scope="col">Lot</th><th scope="col" className="r">Quantity</th><th scope="col" className="r">Effective coverage ({cur})</th></tr></thead>
                  <tbody>{(instr.data.content.legs as { lot_id: string; quantity: string; effective_coverage: string }[]).map(l => (
                    <tr key={l.lot_id}><td className="mono">{l.lot_id}</td><td className="r num">{fmt(l.quantity)}</td><td className="r num">{fmt(l.effective_coverage)}</td></tr>))}</tbody></table></div>
                <div className="row" style={{ marginTop: 8 }}>
                  <button onClick={download}>Download watermarked draft (JSON)</button>
                  <button disabled={!(isMaker || isChecker) || !!p.simulated_settlement} onClick={() => { setErr(null); setAct("settle"); }}>Record SIMULATED settlement…</button>
                </div>
                {p.simulated_settlement && <p className="chip warn" style={{ borderRadius: 8 }}>{p.simulated_settlement.label} at {p.simulated_settlement.at}</p>}
              </div>)}
        </section>
      </div>
      <section className="card" style={{ marginTop: 16 }} aria-labelledby="rev">
        <h2 id="rev">Recommended legs (this revision)</h2>
        {legs.length === 0 ? <EmptyState title="No legs" /> : (
          <div className="scroll-x"><table><caption className="sr-only">Legs</caption><thead><tr><th scope="col">Lot</th><th scope="col" className="r">Quantity</th><th scope="col" className="r">Effective coverage</th><th scope="col" className="r">Horizon cost</th></tr></thead>
            <tbody>{legs.map(l => <tr key={l.lot_id}><td className="mono">{l.lot_id}</td><td className="r num">{fmt(l.quantity)}</td><td className="r num">{fmt(l.effective_coverage)}</td><td className="r num">{fmt(l.cost_horizon)}</td></tr>)}</tbody></table></div>)}
        <p className="faint">Total horizon cost {p.objective ? <MoneyDisplay amount={p.objective} currency={cur} signed={false} /> : "—"} (not annualised; modelled, not realised).</p>
      </section>
      <ConfirmDialog open={act !== null} onClose={() => setAct(null)} pending={pending} requireReason={act === "approve" || act === "cancel"}
        title={act === "approve" ? "Approve allocation proposal" : act === "reserve" ? "Reserve inventory" : act === "cancel" ? "Cancel proposal" : act === "draft" ? "Draft instruction (NOT TRANSMITTED)" : "Record SIMULATED settlement"}
        confirmLabel={act === "approve" ? "Approve" : act === "reserve" ? "Reserve" : act === "cancel" ? "Cancel proposal" : act === "draft" ? "Create draft" : "Record simulation"} onConfirm={exec}>
        {confirmBody}
      </ConfirmDialog>
    </>
  );
}
