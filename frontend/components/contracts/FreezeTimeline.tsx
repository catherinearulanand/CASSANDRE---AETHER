"use client";
import { useState } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { useSession, PINNED_AS_OF } from "@/lib/session";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import type { Approval, PkgDetail, Resolution, TermsView } from "./types";

interface Compare {
  resolved_changes_at_as_of: { key: string; from: unknown; to: unknown }[];
  affected_netting_sets: { netting_set_id: string; master_id: string; member_trades: number }[];
  added_entries: { key: string; effective_from: string; doc_ref: string }[]; removed_entries: { key: string; effective_from: string }[];
}
const amt = (v: unknown) => (v && typeof v === "object" && "amount" in (v as object) ? `${(v as { amount: string }).amount} ${(v as { currency: string }).currency}` : v === undefined ? "—" : JSON.stringify(v));

export default function FreezeTimeline({ pkg, terms, reload }: { pkg: PkgDetail; terms: TermsView; reload: () => Promise<void> }) {
  const { principal } = useSession();
  const roles = principal?.roles ?? [];
  const isMaker = roles.includes("maker"); const isChecker = roles.includes("checker") || roles.includes("compliance_officer");
  const [reason, setReason] = useState(""); const [approval, setApproval] = useState<Approval | null>(null);
  const [err, setErr] = useState<ApiError | null>(null); const [busy, setBusy] = useState(false);
  const [amd, setAmd] = useState({ doc_ref: "", effective_from: "", text: "" });
  const [cmp, setCmp] = useState<{ a: string; b: string; asOf: string }>({ a: "", b: "", asOf: PINNED_AS_OF });
  const [cmpOut, setCmpOut] = useState<Compare | null>(null);
  const [res, setRes] = useState({ asOf: PINNED_AS_OF, knownAt: "", facts: "" }); const [resOut, setResOut] = useState<Resolution | null>(null);
  const rd = terms.readiness;
  const pending: Approval | null = approval ?? rd.pending_approval;  // another session's proposal is visible to the checker

  async function run<T>(fn: () => Promise<T>) { setBusy(true); setErr(null); try { return await fn(); } catch (e) { setErr(e as ApiError); } finally { setBusy(false); } }

  const propose = () => run(async () => { setApproval(await api<Approval>(`/contracts/${pkg.package_id}/freeze/propose`, { method: "POST", idempotencyKey: newKey(), body: { reason } })); await reload(); });
  const freeze = () => run(async () => {
    if (!pending) return;
    await api(`/contracts/${pkg.package_id}/freeze`, { method: "POST", idempotencyKey: newKey(), body: { approval_id: pending.approval_id, reason } });
    setApproval(null); setReason(""); await reload();
  });
  const addAmd = () => run(async () => {
    await api(`/contracts/${pkg.package_id}/amendments`, { method: "POST", idempotencyKey: newKey(), body: { ...amd, title: amd.doc_ref, kind: "AMENDMENT" } });
    setAmd({ doc_ref: "", effective_from: "", text: "" }); await reload();
  });
  const compare = () => run(async () => setCmpOut(await api<Compare>(`/contracts/${pkg.package_id}/versions/compare?a=${cmp.a}&b=${cmp.b}&as_of=${encodeURIComponent(cmp.asOf)}`)));
  const resolve = () => run(async () => {
    const q = new URLSearchParams({ netting_set_id: pkg.netting_set_id, as_of: res.asOf });
    if (res.knownAt) q.set("known_at", res.knownAt); if (res.facts) q.set("fact_snapshot_id", res.facts);
    setResOut(await api<Resolution>(`/contracts/resolve?${q}`));
  });

  return (
    <div className="grid" style={{ gap: 16 }}>
      <section className="card" aria-labelledby="fz-h">
        <h2 id="fz-h">Terms freeze</h2>
        <div className="row"><StatusChip tone={pkg.status === "FROZEN" ? "ok" : "warn"}>{pkg.status.replace(/_/g, " ")}</StatusChip>
          <span className="mono faint">candidate hash {rd.candidate_bundle_hash.slice(0, 16)}…</span>
          {rd.unchanged_from_latest_frozen && <StatusChip tone="neutral">identical to latest frozen version</StatusChip>}</div>
        {rd.blocking_reasons.length > 0 ? <div role="alert" style={{ marginTop: 8 }}><strong>Freeze blocked:</strong>
          <ul>{rd.blocking_reasons.map(b => <li key={b} className="mono" style={{ color: "var(--warn)" }}>{b}</li>)}</ul></div>
          : !rd.unchanged_from_latest_frozen ? <p><StatusChip tone="ok">Ready</StatusChip> All fields are approved or rejected and all required terms are present.</p> : null}
        <label htmlFor="fz-reason">Reason (mandatory)</label>
        <input id="fz-reason" value={reason} onChange={e => setReason(e.target.value)} />
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" disabled={busy || !isMaker || !rd.ready_to_freeze || reason.trim().length < 8} onClick={propose}
            title={isMaker ? "" : "Requires maker role"}>1 · Propose freeze (maker)</button>
          <button disabled={busy || !isChecker || !pending || pending.maker_id === principal?.user_id || reason.trim().length < 8} onClick={freeze}
            title={pending?.maker_id === principal?.user_id ? "An independent checker must approve" : !isChecker ? "Requires checker role" : ""}>2 · Approve exact hash &amp; freeze (checker)</button>
        </div>
        {pending && <div className="card" style={{ marginTop: 8 }} role="region" aria-label="Pending freeze approval">
          <div>Approval <span className="mono">{pending.approval_id.slice(0, 8)}</span> · {pending.decision} · binds revision {pending.target_revision} and hash <span className="mono">{pending.target_hash.slice(0, 16)}…</span></div>
          <div className="muted">Maker {pending.maker_id} · expires {pending.expires_at}. Approval becomes invalid if any field changes.</div></div>}
        {err && <ErrorState error={err} />}
        {err && Object.entries(err.body.field_errors).map(([k, v]) => <div key={k} className="field-error">{v || k}</div>)}
      </section>

      <section className="card" aria-labelledby="tl-h">
        <h2 id="tl-h">Version and amendment timeline</h2>
        {pkg.versions.length === 0 ? <EmptyState title="No frozen version yet" /> : <ol style={{ listStyle: "none", padding: 0 }}>
          {pkg.versions.map(v => (
            <li key={v.version_id} className="card" style={{ marginBottom: 8 }}>
              <div className="row"><strong>v{v.version_no}</strong><StatusChip tone={v.status === "FROZEN" ? "ok" : "neutral"}>{v.status}</StatusChip>
                <span className="mono faint">frozen {v.frozen_at}</span><span className="mono faint">hash {v.bundle_hash.slice(0, 16)}…</span></div>
              <div className="muted">maker {v.maker_id} · checker {v.checker_id} · approval <span className="mono">{v.approval_id.slice(0, 8)}</span>{v.supersedes_version_id && " · supersedes the previous version (history retained)"}</div>
              {v.required_facts.length > 0 && <div className="muted">Needs external facts: <span className="mono">{v.required_facts.join(", ")}</span></div>}
            </li>))}</ol>}
        <h3>Documents</h3>
        <div className="scroll-x" role="region" tabIndex={0} aria-label="Agreement documents (scrollable)"><table><thead><tr><th>Kind</th><th>Reference</th><th>Effective</th><th>Rank</th><th>Extraction</th><th>Original SHA-256</th></tr></thead>
          <tbody>{pkg.document_list.map(d => <tr key={d.doc_id}><td>{d.kind}</td><td className="mono">{d.doc_ref}</td><td className="mono">{d.effective_from.slice(0, 10)}</td>
            <td className="num">{d.rank}</td><td>{d.extracted_mode ?? <StatusChip tone="info">pending</StatusChip>}</td><td className="mono">{d.sha256.slice(0, 16)}…</td></tr>)}</tbody></table></div>
        <p className="faint">Hierarchy: higher rank wins when both are effective (Master 1 &lt; Schedule 2 &lt; CSA 3 &lt; Confirmation 4 &lt; Amendment 5); equal rank → later effective date; equal everything but different values → CONFLICT (blocked, never guessed).</p>
      </section>

      <div className="grid c2" style={{ alignItems: "start" }}>
        <section className="card" aria-labelledby="am-h"><h2 id="am-h">File an amendment</h2>
          {pkg.versions.length === 0 ? <p className="muted">Amendments apply to frozen terms. Freeze the base terms first.</p> : <>
            <label htmlFor="am-ref">Reference</label><input id="am-ref" value={amd.doc_ref} onChange={e => setAmd({ ...amd, doc_ref: e.target.value })} />
            <label htmlFor="am-eff" style={{ marginTop: 8 }}>Effective from (ISO-8601, must match the document header date)</label>
            <input id="am-eff" placeholder="2026-09-01T00:00:00Z" value={amd.effective_from} onChange={e => setAmd({ ...amd, effective_from: e.target.value })} />
            <label htmlFor="am-text" style={{ marginTop: 8 }}>Document text</label>
            <textarea id="am-text" rows={6} className="mono" value={amd.text} onChange={e => setAmd({ ...amd, text: e.target.value })} style={{ width: "100%", padding: 8 }} />
            <button style={{ marginTop: 8 }} disabled={busy || !isMaker || !amd.doc_ref || !amd.text || !amd.effective_from} onClick={addAmd} title={isMaker ? "" : "Requires maker role"}>Add amendment</button>
            <p className="faint">It then goes through extraction, per-field maker/checker review and a new freeze. The earlier version stays reproducible.</p></>}
        </section>
        <section className="card" aria-labelledby="cmp-h"><h2 id="cmp-h">Compare versions</h2>
          {pkg.versions.length < 2 ? <p className="muted">At least two frozen versions are needed.</p> : <>
            <div className="row"><div><label htmlFor="cmp-a">From</label><select id="cmp-a" value={cmp.a} onChange={e => setCmp({ ...cmp, a: e.target.value })}><option value="">—</option>{pkg.versions.map(v => <option key={v.version_id} value={v.version_id}>v{v.version_no}</option>)}</select></div>
              <div><label htmlFor="cmp-b">To</label><select id="cmp-b" value={cmp.b} onChange={e => setCmp({ ...cmp, b: e.target.value })}><option value="">—</option>{pkg.versions.map(v => <option key={v.version_id} value={v.version_id}>v{v.version_no}</option>)}</select></div>
              <div><label htmlFor="cmp-t">As of</label><input id="cmp-t" value={cmp.asOf} onChange={e => setCmp({ ...cmp, asOf: e.target.value })} /></div></div>
            <button style={{ marginTop: 8 }} disabled={!cmp.a || !cmp.b || busy} onClick={compare}>Compare</button>
            {cmpOut && <div style={{ marginTop: 8 }}>
              {cmpOut.resolved_changes_at_as_of.length === 0 ? <p className="muted">No change in resolved terms at this as-of date.</p> :
                <table><caption className="sr-only">Resolved changes</caption><thead><tr><th>Term</th><th>From</th><th>To</th></tr></thead><tbody>
                  {cmpOut.resolved_changes_at_as_of.map(c => <tr key={c.key}><td>{c.key}</td><td className="num">{amt(c.from)}</td><td className="num">{amt(c.to)}</td></tr>)}</tbody></table>}
              <div className="muted">Affected netting sets: {cmpOut.affected_netting_sets.map(n => `${n.netting_set_id} (${n.member_trades} trades)`).join(", ")}</div></div>}</>}
        </section>
      </div>

      <section className="card" aria-labelledby="rs-h"><h2 id="rs-h">Resolve terms as-of (what downstream engines receive)</h2>
        <div className="grid c3">
          <div><label htmlFor="rs-asof">As of (business time)</label><input id="rs-asof" value={res.asOf} onChange={e => setRes({ ...res, asOf: e.target.value })} /></div>
          <div><label htmlFor="rs-known">Known at (recording time, optional)</label><input id="rs-known" value={res.knownAt} onChange={e => setRes({ ...res, knownAt: e.target.value })} /></div>
          <div><label htmlFor="rs-facts">Fact snapshot ID (optional)</label><input id="rs-facts" value={res.facts} onChange={e => setRes({ ...res, facts: e.target.value })} /></div></div>
        <button style={{ marginTop: 8 }} onClick={resolve} disabled={busy}>Resolve</button>
        {resOut && (resOut.status === "RESOLVED" ? <div style={{ marginTop: 8 }}>
          <StatusChip tone="ok">RESOLVED</StatusChip> v{resOut.version_no} · terms hash <span className="mono">{resOut.terms_hash?.slice(0, 20)}…</span>
          <pre className="mono" style={{ fontSize: 12, overflow: "auto" }}>{JSON.stringify({ ...resOut.terms, eligible_schedule: `[${(resOut.terms?.eligible_schedule as unknown[] | undefined)?.length ?? 0} rows]` }, null, 1)}</pre>
          <div className="faint">{resOut.regulatory_constraints}</div></div>
          : <div role="alert" style={{ marginTop: 8 }}><StatusChip tone="danger">BLOCKED — no authoritative terms</StatusChip>
            <ul>{resOut.blocking_reasons.map(b => <li key={b} className="mono">{b}</li>)}</ul>{resOut.required_facts.length > 0 && <div>Required external facts: <span className="mono">{resOut.required_facts.join(", ")}</span></div>}</div>)}
      </section>
    </div>
  );
}
