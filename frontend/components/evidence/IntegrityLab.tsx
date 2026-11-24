"use client";
import { useState } from "react";
import { api, newKey } from "@/lib/api";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import RunPicker from "./RunPicker";
import { useLoad } from "./useLoad";
import type { ExportOut, Integrity, ReplayOut, TamperOut, VerifyResult } from "./types";

const MODES: [string, string][] = [["none", "No change (control)"], ["modify_event", "Modify an event"], ["drop_event", "Drop an event"],
  ["truncate_chain_skeleton", "Truncate chain tail"], ["wrong_trusted_key", "Verify with the wrong key"]];
const tone = (s: string) => (s === "OK" || s === "MATCH" ? "ok" : s === "DEGRADED" || s === "MATCH_WITHIN_TOLERANCE" || s === "NOTHING_TO_REPLAY" ? "warn" : "danger") as "ok" | "warn" | "danger";

export default function IntegrityLab({ runId, setRunId }: { runId: string; setRunId: (id: string) => void }) {
  const dash = useLoad<Integrity>(s => api("/evidence/integrity", { signal: s }), []);
  const [msg, setMsg] = useState<Error | null>(null);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [replay, setReplay] = useState<ReplayOut | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [exp, setExp] = useState<ExportOut | null>(null);
  const [mode, setMode] = useState("modify_event");
  const [tamper, setTamper] = useState<TamperOut | null>(null);

  async function act<T>(name: string, fn: () => Promise<T>, set: (v: T) => void) {
    setBusy(name); setMsg(null);
    try { set(await fn()); } catch (e) { setMsg(e as Error); } finally { setBusy(null); }
  }
  const d = dash.data;
  return (
    <div className="grid" style={{ gap: 16 }}>
      <section className="card" aria-labelledby="int-h">
        <div className="row"><h2 id="int-h" style={{ margin: 0 }}>Integrity dashboard</h2><span className="spacer" />
          <button onClick={() => act("cp", () => api("/evidence/checkpoints", { method: "POST", idempotencyKey: newKey() }), () => dash.reload())} disabled={busy === "cp"}>
            {busy === "cp" ? "Signing…" : "Create signed checkpoint"}</button>
          <button onClick={dash.reload}>Refresh</button></div>
        {dash.error ? <ErrorState error={dash.error} /> : dash.loading || !d ? <EmptyState title="Checking integrity…" /> : (
          <div aria-live="polite">
            <StatusChip tone={tone(d.status)}>{d.status}</StatusChip>
            {d.problems.length > 0 && <ul>{d.problems.map(p => <li key={p}>{p}</li>)}</ul>}
            <div className="grid c3" style={{ marginTop: 12 }}>
              <div><h3>Chain</h3><p className="mono">{d.chain.events} events · head seq {d.head_sequence}<br />anchor: {d.chain.anchor_status}<br />since last checkpoint: {d.events_since_last_checkpoint}</p></div>
              <div><h3>Vault &amp; storage</h3><p className="mono">{d.objects.objects} objects · {d.objects.valid ? "all hashes match" : "PROBLEMS"}<br />storage {d.storage.writable ? "writable" : "NOT writable"}</p></div>
              <div><h3>Signing &amp; outbox</h3><p className="mono">key {d.signing.current_key_id}<br />{d.signing.protection}<br />outbox pending {d.outbox.pending} · dead {d.outbox.dead}</p></div>
            </div>
            <p className="muted">Local file key, not an HSM. A hash chain alone cannot detect truncation; only a signed checkpoint held outside the database can.</p>
          </div>)}
      </section>

      <section className="card" aria-labelledby="rp-h"><h2 id="rp-h">Verify and read-only replay</h2>
        <div className="row" style={{ alignItems: "end" }}>
          <RunPicker value={runId} onChange={setRunId} />
          <button disabled={busy === "verify"} onClick={() => act("verify", () => api<VerifyResult>("/evidence/verify", { method: "POST", body: { anchor: "exported_checkpoints" } }), setVerify)}>Verify chain</button>
          <button className="primary" disabled={!runId || busy === "replay"} onClick={() => act("replay", () => api<ReplayOut>("/evidence/replay", { method: "POST", body: { run_id: runId }, idempotencyKey: newKey() }), setReplay)}>
            {busy === "replay" ? "Replaying…" : "Replay (read-only)"}</button>
        </div>
        {msg && <div style={{ marginTop: 8 }}><ErrorState error={msg} /></div>}
        <div aria-live="polite" style={{ marginTop: 12 }}>
          {verify && <p><StatusChip tone={verify.valid ? "ok" : "danger"}>{verify.valid ? "Chain valid" : `Chain invalid (${verify.problems.length})`}</StatusChip> <span className="mono">{verify.events} events · {verify.anchor_status}</span></p>}
          {replay && (<>
            <p><StatusChip tone={tone(replay.status)}>{replay.status}</StatusChip> <span className="muted">replay recorded as its own event; no live effects (rolled back)</span></p>
            {replay.status === "NOT_REPLAYABLE" && <p className="muted">An engine/method version or replayer is missing, so no result is claimed. The current version is never substituted.</p>}
            {replay.caveats.map((c, i) => <p key={i} className="muted">{c}</p>)}
            {replay.items.length > 0 && <div className="scroll-x"><table><thead><tr><th>Seq</th><th>Engine</th><th>Action</th><th>Result</th><th>Reason</th></tr></thead><tbody>
              {replay.items.map(i => <tr key={i.sequence}><td className="num">{i.sequence}</td><td>{i.engine}</td><td>{i.action}</td><td><StatusChip tone={tone(i.status)}>{i.status}</StatusChip></td><td>{i.reason ?? "—"}</td></tr>)}</tbody></table></div>}
          </>)}
        </div>
      </section>

      <section className="card" aria-labelledby="tp-h"><h2 id="tp-h">Tamper demonstration (copied bundle only)</h2>
        <p className="muted">Creates an export, then alters an in-memory <strong>copy</strong> and runs the verifier. Operational evidence is never modified.</p>
        <div className="row" style={{ alignItems: "end" }}>
          <button disabled={!runId || busy === "exp"} onClick={() => act("exp", () => api<ExportOut>("/evidence/export", { method: "POST", body: { run_id: runId }, idempotencyKey: newKey() }), v => { setExp(v); setTamper(null); })}>
            {busy === "exp" ? "Exporting…" : exp ? "Create another export" : "Create export for selected run"}</button>
          <div style={{ minWidth: 220 }}><label htmlFor="tmode">Alteration</label>
            <select id="tmode" value={mode} onChange={e => setMode(e.target.value)}>{MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></div>
          <button className="primary" disabled={!exp || busy === "tamper"} onClick={() => exp && act("tamper", () => api<TamperOut>("/evidence/tamper-demo", { method: "POST", body: { export_id: exp.export_id, mode } }), setTamper)}>Run verifier on copy</button>
        </div>
        {exp && <p className="mono muted">export {exp.export_id.slice(0, 8)} · {exp.events} events{exp.redacted ? " · REDACTED" : ""}</p>}
        <div aria-live="polite">{tamper && (<>
          <StatusChip tone={tamper.verified ? "ok" : "danger"}>{tamper.verified ? "Verified" : "Verification FAILED (expected for an altered copy)"}</StatusChip>
          <ul className="mono">{tamper.problems.map((p, i) => <li key={i}>{p}</li>)}</ul><p className="muted">{tamper.note}</p></>)}</div>
      </section>
    </div>
  );
}
