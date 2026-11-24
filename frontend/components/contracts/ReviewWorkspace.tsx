"use client";
import { useEffect, useMemo, useState } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, MoneyDisplay, ProvenanceBadge, StatusChip } from "@/components/ui";
import { isMoney, type Field, type FieldValue, type PkgDetail, type TermsView } from "./types";

const TONE = { PROPOSED: "info", UNRESOLVED: "danger", APPROVED: "ok", REJECTED: "neutral" } as const;
const DIRECTIONAL: [string, string][] = [["threshold", "Threshold"], ["mta", "Minimum Transfer Amount"], ["independent_amount", "Independent Amount"]];

export function ValueView({ v }: { v: FieldValue }) {
  if (v === null || v === undefined) return <span className="faint">—</span>;
  if (isMoney(v)) return <MoneyDisplay amount={v.amount} currency={v.currency} signed={false} />;
  if (Array.isArray(v)) return <span>{v.length} schedule rows</span>;
  return <span className="mono">{String(v)}</span>;
}

function Condition({ c }: { c: Field["condition"] }) {
  if (!c) return null;
  return <div className="muted" style={{ fontSize: 12 }}>Conditional: if <span className="mono">{String(c.fact)}</span> {String(c.op)} <span className="mono">{String(c.value)}</span> (external fact required)</div>;
}

function DocViewer({ pkgId, docId, selected }: { pkgId: string; docId: string; selected: Field | null }) {
  const [doc, setDoc] = useState<{ text: string; doc_ref: string; sha256: string; kind: string; missing_annexes: string[] } | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => { setDoc(null); setErr(null); api<typeof doc>(`/contracts/${pkgId}/documents/${docId}`).then(setDoc).catch(setErr); }, [pkgId, docId]);
  useEffect(() => { document.getElementById("hl")?.scrollIntoView({ block: "center" }); }, [selected, doc]);
  if (err) return <ErrorState error={err} />;
  if (!doc) return <EmptyState title="Loading source…" />;
  const hl = selected && selected.doc_id === docId ? selected : null;
  const t = doc.text;
  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}><strong>{doc.doc_ref}</strong><StatusChip tone="neutral">{doc.kind}</StatusChip>
        <span className="mono faint" title="SHA-256 of the immutable original">sha256 {doc.sha256.slice(0, 12)}…</span></div>
      {doc.missing_annexes.length > 0 && <div role="alert"><StatusChip tone="danger">Missing annex</StatusChip> {doc.missing_annexes.join("; ")}</div>}
      <pre className="mono" tabIndex={0} aria-label={`Source text of ${doc.doc_ref}`}
        style={{ whiteSpace: "pre-wrap", background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 8, padding: 12, maxHeight: 460, overflow: "auto", fontSize: 12 }}>
        {hl ? <>{t.slice(0, hl.start_char)}<mark id="hl" style={{ background: "var(--accent-wash)", color: "var(--accent)", outline: "1px solid var(--accent)" }}>{t.slice(hl.start_char, hl.end_char)}</mark>{t.slice(hl.end_char)}</> : t}
      </pre>
      {hl && <div className="mono faint" style={{ marginTop: 4 }}>Citation: chars {hl.start_char}–{hl.end_char} (exact quote verified server-side)</div>}
    </div>
  );
}

export default function ReviewWorkspace({ pkg, terms, reload }: { pkg: PkgDetail; terms: TermsView; reload: () => Promise<void> }) {
  const { principal } = useSession();
  const roles = principal?.roles ?? [];
  const isMaker = roles.includes("maker");
  const isChecker = roles.includes("checker") || roles.includes("compliance_officer");
  const [sel, setSel] = useState<Field | null>(null);
  const [reason, setReason] = useState(""); const [corr, setCorr] = useState("");
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<ApiError | null>(null);
  const [mode, setMode] = useState<"RULES" | "REPLAY">("RULES");
  const [docId, setDocId] = useState(pkg.document_list[0]?.doc_id ?? "");

  const fields = terms.fields;
  const current = useMemo(() => fields.find(f => f.field_id === sel?.field_id) ?? null, [fields, sel]);
  useEffect(() => { if (current && current.doc_id !== docId) setDocId(current.doc_id); }, [current, docId]);
  useEffect(() => { setCorr(current ? JSON.stringify(current.value, null, 1) : ""); setReason(""); setErr(null); }, [current?.field_id, current?.revision]);

  async function act(action: "APPROVE" | "REJECT" | "MARK_UNRESOLVED" | "CORRECT") {
    if (!current) return;
    setBusy(true); setErr(null);
    try {
      let corrected: unknown = undefined;
      if (action === "CORRECT") corrected = JSON.parse(corr);
      await api(`/contracts/${pkg.package_id}/review`, { method: "POST", idempotencyKey: newKey(),
        body: { field_id: current.field_id, action, reason, expected_revision: current.revision, corrected_value: corrected } });
      await reload();
    } catch (e) { setErr(e instanceof ApiError ? e : new ApiError(0, { code: "CLIENT", message: String(e), field_errors: {}, correlation_id: "n/a", retryable: false })); }
    finally { setBusy(false); }
  }
  async function extract() {
    setBusy(true); setErr(null);
    try { await api(`/contracts/${pkg.package_id}/extract`, { method: "POST", idempotencyKey: newKey(), body: { mode } }); await reload(); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }

  const unresolved = fields.filter(f => f.status === "UNRESOLVED");
  const byKey = (k: string, d: "A" | "B") => fields.filter(f => f.field_key === `${k}_${d.toLowerCase()}`);
  const frozenSel = !!current?.frozen;
  const canApprove = isChecker && current?.status === "PROPOSED" && !frozenSel && current.maker_id !== principal?.user_id;

  return (
    <div className="grid" style={{ gap: 16 }}>
      {pkg.docs_pending_extraction > 0 && (
        <div className="card row" role="region" aria-label="Extraction">
          <StatusChip tone="info">{pkg.docs_pending_extraction} document(s) not yet extracted</StatusChip>
          <label htmlFor="mode" className="sr-only">Extraction mode</label>
          <select id="mode" value={mode} onChange={e => setMode(e.target.value as "RULES" | "REPLAY")} style={{ width: 280 }}>
            <option value="RULES">RULES — deterministic clause extractor</option><option value="REPLAY">REPLAY — hand-labelled fixture</option></select>
          <button className="primary" disabled={!isMaker || busy} onClick={extract} title={isMaker ? "" : "Requires maker role"}>{busy ? "Extracting…" : "Extract proposals"}</button>
          <span className="muted">LIVE_MODEL is not implemented in this build. Nothing extracted is approved automatically.</span>
        </div>)}

      <div className="card" aria-labelledby="dir-h">
        <h2 id="dir-h">Directional terms (Party A / Party B)</h2>
        <div className="scroll-x"><table>
          <caption className="sr-only">Thresholds, minimum transfer amounts and independent amounts by party</caption>
          <thead><tr><th>Term</th><th>Party A — {pkg.party_a_name}</th><th>Party B — {pkg.party_b_name}</th></tr></thead>
          <tbody>{DIRECTIONAL.map(([k, label]) => (
            <tr key={k}>{[<td key="l">{label}</td>, ...(["A", "B"] as const).map(d => (
              <td key={d}>{byKey(k, d).length === 0 ? <StatusChip tone="danger">missing</StatusChip> : byKey(k, d).map(f => (
                <div key={f.field_id}><button onClick={() => setSel(f)} aria-label={`Open ${label} for party ${d}`} style={{ minHeight: 28, padding: "0 8px" }}><ValueView v={f.value} /></button>{" "}
                  <StatusChip tone={TONE[f.status]}>{f.status}</StatusChip><Condition c={f.condition} /></div>))}</td>))]}</tr>))}</tbody></table></div>
        <p className="faint">Direction is taken from the party named in the quoted clause, never from table position. Missing required terms block freezing.</p>
      </div>

      {unresolved.length > 0 && <div className="card" role="region" aria-label="Unresolved field queue">
        <h2>Unresolved field queue ({unresolved.length})</h2>
        <ul>{unresolved.map(f => <li key={f.field_id}><button onClick={() => setSel(f)}>{f.field_key}{f.direction ? ` (${f.direction})` : ""}</button> <span className="muted">{f.issues.join(" · ")}</span></li>)}</ul></div>}

      <div className="grid c2" style={{ alignItems: "start" }}>
        <section className="card" aria-labelledby="src-h"><h2 id="src-h">Source document</h2>
          <label htmlFor="docsel">Document</label>
          <select id="docsel" value={docId} onChange={e => setDocId(e.target.value)} style={{ marginBottom: 8 }}>
            {pkg.document_list.map(d => <option key={d.doc_id} value={d.doc_id}>{d.kind} · {d.doc_ref} · eff {d.effective_from.slice(0, 10)}</option>)}</select>
          {docId && <DocViewer pkgId={pkg.package_id} docId={docId} selected={current} />}
        </section>
        <section className="card" aria-labelledby="fld-h"><h2 id="fld-h">Structured terms ({fields.length})</h2>
          {fields.length === 0 ? <EmptyState title="No extracted fields yet" /> :
            <div className="scroll-x" style={{ maxHeight: 300, overflow: "auto" }}><table>
              <caption className="sr-only">Extracted fields with status</caption>
              <thead><tr><th>Field</th><th>Dir</th><th>Value</th><th>Status</th><th>Mode</th></tr></thead>
              <tbody>{fields.map(f => (
                <tr key={f.field_id} aria-selected={current?.field_id === f.field_id}>
                  <td><button style={{ minHeight: 28 }} className={current?.field_id === f.field_id ? "primary" : ""} onClick={() => setSel(f)}>{f.field_key}</button>{f.material && <span className="faint"> material</span>}</td>
                  <td className="mono">{f.direction ?? "—"}</td><td><ValueView v={f.value} /></td>
                  <td><StatusChip tone={TONE[f.status]}>{f.status}</StatusChip>{f.frozen && <> <StatusChip tone="neutral">frozen</StatusChip></>}</td>
                  <td><ProvenanceBadge kind={f.extraction_mode === "REPLAY" ? "REPLAY" : "MANUAL"} />{f.extraction_mode === "RULES" && <span className="faint"> rules</span>}</td></tr>))}</tbody></table></div>}
          {current && (
            <div style={{ marginTop: 12, borderTop: "1px solid var(--border)", paddingTop: 12 }} role="region" aria-label="Field review">
              <h3>Review: {current.field_key}{current.direction ? ` · Party ${current.direction}` : ""}</h3>
              <blockquote className="mono" style={{ margin: "4px 0", fontSize: 12, color: "var(--text-2)" }}>“{current.quote.length > 220 ? current.quote.slice(0, 220) + "…" : current.quote}”</blockquote>
              <div className="faint mono">chars {current.start_char}–{current.end_char} · from {current.doc_ref} · maker {current.maker_id}{current.reviewer_id ? ` · reviewer ${current.reviewer_id}` : ""} · rev {current.revision}</div>
              {current.issues.length > 0 && <ul role="alert" style={{ color: "var(--danger)" }}>{current.issues.map(i => <li key={i}>{i}</li>)}</ul>}
              {current.original_value !== null && JSON.stringify(current.original_value) !== JSON.stringify(current.value) && <div className="muted">Original extraction (preserved): <ValueView v={current.original_value} /></div>}
              {current.review_reason && <div className="muted">Last reason: {current.review_reason}</div>}
              {frozenSel ? <StatusChip tone="neutral">Frozen terms are read-only — file an amendment to change them</StatusChip> : <>
                <label htmlFor="reason" style={{ marginTop: 8 }}>Reason (mandatory, ≥ 8 characters)</label>
                <textarea id="reason" rows={2} value={reason} onChange={e => setReason(e.target.value)} style={{ width: "100%", padding: 8 }} />
                <label htmlFor="corr" style={{ marginTop: 8 }}>Corrected value (JSON; decimals as strings) — maker only</label>
                <textarea id="corr" rows={3} className="mono" value={corr} onChange={e => setCorr(e.target.value)} style={{ width: "100%", padding: 8 }} />
                <div className="row" style={{ marginTop: 8 }}>
                  <button className="primary" disabled={busy || !canApprove || reason.trim().length < 8} onClick={() => act("APPROVE")}
                    title={!isChecker ? "Requires checker role" : current.maker_id === principal?.user_id ? "You made this change; an independent checker must approve" : ""}>Approve</button>
                  <button disabled={busy || !isMaker || reason.trim().length < 8} onClick={() => act("CORRECT")}>Correct</button>
                  <button disabled={busy || !isChecker || reason.trim().length < 8} onClick={() => act("REJECT")}>Reject</button>
                  <button disabled={busy || reason.trim().length < 8} onClick={() => act("MARK_UNRESOLVED")}>Mark unresolved</button>
                </div>
                {!canApprove && isChecker && current.status !== "PROPOSED" && <div className="muted">Only validated PROPOSED fields can be approved.</div>}
              </>}
              {err && <ErrorState error={err} />}
              {err && Object.keys(err.body.field_errors).length > 0 && <ul>{Object.entries(err.body.field_errors).map(([k, v]) => <li key={k} className="field-error">{k}: {v}</li>)}</ul>}
            </div>)}
        </section>
      </div>
    </div>
  );
}
