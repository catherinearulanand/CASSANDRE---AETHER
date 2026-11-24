"use client";
import { useState } from "react";
import { api, newKey } from "@/lib/api";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import RunPicker from "./RunPicker";
import { useLoad } from "./useLoad";
import type { ExportDetail, ExportOut, PublicKey } from "./types";

export default function ExportsView({ runId, setRunId }: { runId: string; setRunId: (id: string) => void }) {
  const list = useLoad<{ items: ExportOut[] }>(s => api(`/evidence/exports${runId ? `?run_id=${runId}` : ""}`, { signal: s }), [runId]);
  const keys = useLoad<{ items: PublicKey[]; warning: string }>(s => api("/evidence/public-keys", { signal: s }), []);
  const [redact, setRedact] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<Error | null>(null);
  const [sel, setSel] = useState<ExportDetail | null>(null);
  const [confirm, setConfirm] = useState(false);

  async function create() {
    setBusy(true); setErr(null); setConfirm(false);
    try { const o = await api<ExportOut>("/evidence/export", { method: "POST", body: { run_id: runId, redact }, idempotencyKey: newKey() }); list.reload(); setSel(await api<ExportDetail>(`/evidence/exports/${o.export_id}`)); }
    catch (e) { setErr(e as Error); } finally { setBusy(false); }
  }
  async function open(id: string) { setErr(null); try { setSel(await api<ExportDetail>(`/evidence/exports/${id}`)); } catch (e) { setErr(e as Error); } }
  const cur = keys.data?.items.find(k => k.current);

  return (
    <div className="grid" style={{ gap: 16 }}>
      <section className="card" aria-labelledby="ex-h"><h2 id="ex-h">Create evidence export</h2>
        <div className="row" style={{ alignItems: "end" }}><RunPicker value={runId} onChange={setRunId} label="Run" />
          <label style={{ display: "flex", gap: 8, alignItems: "center", margin: 0 }}><input type="checkbox" style={{ width: 20, minHeight: 20 }} checked={redact} onChange={e => setRedact(e.target.checked)} />Redact event content</label>
          <button className="primary" disabled={!runId || busy} onClick={() => setConfirm(true)}>{busy ? "Building…" : "Export signed bundle"}</button></div>
        {confirm && (<div role="alertdialog" aria-label="Confirm export" className="card" style={{ marginTop: 12, background: "var(--elevated)" }}>
          <p style={{ marginTop: 0 }}>Create a {redact ? <strong>redacted</strong> : "full"} signed bundle (JSON + CSV + manifest) for run <span className="mono">{runId.slice(0, 8)}</span>? This signs a new checkpoint and records an EVIDENCE_EXPORTED event. Your role may force redaction.</p>
          <div className="row"><button className="primary" onClick={create}>Confirm export</button><button onClick={() => setConfirm(false)}>Cancel</button></div></div>)}
        {err && <div style={{ marginTop: 8 }}><ErrorState error={err} /></div>}
        <p className="muted">This is an <strong>internal evidence pack</strong>, not a regulatory submission. No reporting-schema adapter is implemented in this build, so no report is validated against any official format and nothing is transmitted.</p>
      </section>

      <section className="card" aria-labelledby="exl-h"><h2 id="exl-h">Exports</h2>
        {list.error ? <ErrorState error={list.error} /> : list.loading ? <EmptyState title="Loading…" /> : !list.data || list.data.items.length === 0 ? <EmptyState title="No exports yet" />
          : <div className="scroll-x"><table><thead><tr><th>Export</th><th>Run</th><th>Events</th><th>Content</th><th>Created</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>
            {list.data.items.map(e => <tr key={e.export_id}><td className="mono">{e.export_id.slice(0, 8)}</td><td className="mono">{e.run_id.slice(0, 8)}</td><td className="num">{e.events}</td>
              <td>{e.redacted ? <StatusChip tone="warn">Redacted</StatusChip> : <StatusChip tone="info">Full</StatusChip>}</td><td className="mono">{e.created_at}</td>
              <td><button onClick={() => open(e.export_id)}>Preview</button> <a className="btn" style={{ display: "inline-block", lineHeight: "36px", padding: "0 12px", border: "1px solid var(--border)", borderRadius: 6 }} href={`/api/v1/evidence/exports/${e.export_id}/download`}>Download</a></td></tr>)}
          </tbody></table></div>}
      </section>

      {sel && (<section className="card" aria-labelledby="pv-h"><h2 id="pv-h">Preview · export {sel.export_id.slice(0, 8)}</h2>
        <p>{sel.redacted ? <StatusChip tone="warn">REDACTED: {sel.preview.redaction_note}</StatusChip> : <StatusChip tone="info">Full content</StatusChip>}</p>
        <p className="mono">checkpoint seq {sel.preview.chain.checkpoint_sequence} · key {sel.preview.chain.key_id}<br />bundle sha256 {sel.bundle_hash}</p>
        <div className="scroll-x"><table><thead><tr><th>File</th><th className="r">Bytes</th><th>SHA-256</th></tr></thead><tbody>
          {Object.entries(sel.preview.files).map(([n, m]) => <tr key={n}><td className="mono">{n}</td><td className="r num">{m.size}</td><td className="mono">{m.sha256.slice(0, 16)}…</td></tr>)}</tbody></table></div>
        {sel.preview.missing_objects.length > 0 && <><h3>Missing objects (reported, not hidden)</h3><ul className="mono">{sel.preview.missing_objects.map(m => <li key={m.content_hash}>{m.content_hash.slice(0, 16)}… — {m.reason}</li>)}</ul></>}
        <h3>Limitations</h3><ul className="muted">{sel.preview.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
        <h3>Verify offline</h3>
        <p className="muted">Give the verifier the bundle and a public key you obtained out-of-band. A non-zero exit means tampering or an unsafe bundle.</p>
        <pre className="mono" style={{ overflow: "auto" }}>python -m tools.verify_bundle aether-evidence-{sel.export_id.slice(0, 8)}.zip --trusted-key trusted.pub</pre>
        {cur && <details><summary>Current public key ({cur.key_id}, {cur.protection})</summary><pre className="mono" style={{ overflow: "auto" }}>{cur.public_key_pem}</pre></details>}
      </section>)}
    </div>
  );
}
