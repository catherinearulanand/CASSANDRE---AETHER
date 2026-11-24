"use client";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, ProvenanceBadge, StatusChip } from "@/components/ui";
import { useLoad } from "./useLoad";
import type { EventsPage, EvEventOut, VerifyResult } from "./types";

interface Filters { actor_id: string; action: string; engine: string; correlation_id: string; entity_id: string }
const EMPTY: Filters = { actor_id: "", action: "", engine: "", correlation_id: "", entity_id: "" };

function qs(f: Filters, cursor: number | null) {
  const p = new URLSearchParams();
  Object.entries(f).forEach(([k, v]) => v && p.set(k, v));
  if (cursor) p.set("cursor", String(cursor));
  p.set("limit", "25");
  return p.toString();
}

export default function AuditExplorer({ initialCorrelation }: { initialCorrelation?: string }) {
  const [draft, setDraft] = useState<Filters>({ ...EMPTY, correlation_id: initialCorrelation ?? "" });
  const [filters, setFilters] = useState<Filters>({ ...EMPTY, correlation_id: initialCorrelation ?? "" });
  const [cursors, setCursors] = useState<(number | null)[]>([null]); // server cursor stack for paging back
  const cursor = cursors[cursors.length - 1];
  const { data, error, loading } = useLoad<EventsPage>(s => api(`/evidence/events?${qs(filters, cursor)}`, { signal: s }), [filters, cursor]);
  const [sel, setSel] = useState<EvEventOut | null>(null);
  const [ver, setVer] = useState<VerifyResult | null>(null);
  const [verErr, setVerErr] = useState<Error | null>(null);
  const [verBusy, setVerBusy] = useState(false);

  function apply(e: FormEvent) { e.preventDefault(); setCursors([null]); setFilters(draft); setSel(null); }
  async function verify() {
    setVerBusy(true); setVerErr(null);
    try { setVer(await api<VerifyResult>("/evidence/verify", { method: "POST", body: { anchor: "exported_checkpoints" } })); }
    catch (e) { setVerErr(e as Error); setVer(null); } finally { setVerBusy(false); }
  }
  const f = (k: keyof Filters, label: string) => (
    <div style={{ minWidth: 140, flex: 1 }}><label htmlFor={`f-${k}`}>{label}</label>
      <input id={`f-${k}`} value={draft[k]} onChange={e => setDraft({ ...draft, [k]: e.target.value })} /></div>);

  return (
    <div className="grid" style={{ gap: 16 }}>
      <section className="card" aria-labelledby="chain-h">
        <div className="row"><h2 id="chain-h" style={{ margin: 0 }}>Chain verification</h2><span className="spacer" />
          <button onClick={verify} disabled={verBusy}>{verBusy ? "Verifying…" : "Verify chain against signed checkpoints"}</button></div>
        <div aria-live="polite" style={{ marginTop: 8 }}>
          {verErr && <ErrorState error={verErr} />}
          {ver && (ver.valid
            ? <StatusChip tone="ok">Chain valid · {ver.events} events · anchor {ver.anchor_status}</StatusChip>
            : <><StatusChip tone="danger">Chain INVALID · {ver.problems.length} problem(s)</StatusChip>
              <ul className="mono">{ver.problems.slice(0, 8).map((p, i) => <li key={i}>{p.problem}{p.sequence ? ` @seq ${p.sequence}` : ""} {p.detail ?? ""}</li>)}</ul></>)}
          {ver && ver.anchor_status === "NO_TRUSTED_ANCHOR" && <p className="muted">No signed exported checkpoint exists, so truncation cannot be detected. Create a checkpoint in the Integrity lab.</p>}
          {!ver && !verErr && <p className="muted" style={{ margin: 0 }}>Requires auditor, compliance officer or admin role.</p>}
        </div>
      </section>

      <form className="card" onSubmit={apply} aria-label="Filter audit events">
        <div className="row" style={{ alignItems: "end" }}>
          {f("actor_id", "Actor ID")}{f("action", "Action")}{f("engine", "Engine")}{f("correlation_id", "Correlation ID")}{f("entity_id", "Entity ID")}
          <button className="primary" type="submit">Search</button>
          <button type="button" onClick={() => { setDraft(EMPTY); setFilters(EMPTY); setCursors([null]); }}>Clear</button>
        </div>
      </form>

      <section className="card" aria-labelledby="ev-h">
        <div className="row"><h2 id="ev-h" style={{ margin: 0 }}>Audit events</h2>
          {data && <StatusChip tone={data.detail_level === "FULL" ? "info" : "warn"}>{data.detail_level === "FULL" ? "Full detail" : "Metadata only (role)"}</StatusChip>}
          <span className="spacer" /><ProvenanceBadge kind="SYNTHETIC" /></div>
        {error ? <ErrorState error={error} /> : loading ? <EmptyState title="Loading events…" /> : !data || data.items.length === 0
          ? <EmptyState title="No events match">Events appear when engines record consequential actions.</EmptyState>
          : <div className="scroll-x"><table><caption className="sr-only">Evidence events, newest first</caption>
            <thead><tr><th scope="col">Seq</th><th scope="col">Recorded (UTC)</th><th scope="col">Engine</th><th scope="col">Action</th><th scope="col">Entity</th><th scope="col">Correlation</th><th scope="col">Hash</th></tr></thead>
            <tbody>{data.items.map(e => (
              <tr key={e.sequence} aria-selected={sel?.sequence === e.sequence}>
                <td className="num"><button onClick={() => setSel(e)} aria-label={`Open details for event ${e.sequence}`} style={{ minHeight: 32 }}>{e.sequence}</button></td>
                <td className="mono">{e.recorded_at}</td><td>{e.engine}</td><td>{e.action}</td>
                <td className="mono">{e.entity_type}:{e.entity_id}</td><td className="mono">{e.correlation_id.slice(0, 8)}</td>
                <td className="mono" title={e.event_hash}>{e.event_hash.slice(0, 10)}…</td></tr>))}</tbody></table></div>}
        <div className="row" style={{ marginTop: 12 }}>
          <button disabled={cursors.length <= 1 || loading} onClick={() => setCursors(c => c.slice(0, -1))}>Newer</button>
          <button disabled={!data?.next_cursor || loading} onClick={() => data?.next_cursor && setCursors(c => [...c, data.next_cursor])}>Older</button>
        </div>
      </section>

      {sel && (
        <aside className="card" role="region" aria-label={`Event ${sel.sequence} technical details`}>
          <div className="row"><h2 style={{ margin: 0 }}>Event {sel.sequence} · technical details</h2><span className="spacer" /><button onClick={() => setSel(null)}>Close</button></div>
          <dl className="mono" style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 16px", overflowWrap: "anywhere" }}>
            {([["event_id", sel.event_id], ["service", sel.service_version], ["source mode", sel.source_mode], ["effective_at", sel.effective_at],
              ["recorded_at", sel.recorded_at], ["prev_hash", sel.prev_hash], ["event_hash", sel.event_hash], ["actor", sel.actor_id ?? "(hidden for your role)"],
              ["rationale", sel.rationale ?? "(hidden or none)"], ["output_hash", sel.output_hash ?? "—"]] as [string, string][]).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt className="muted">{k}</dt><dd style={{ margin: 0 }}>{v}</dd></div>)}
          </dl>
          {sel.payload && <pre className="mono" style={{ overflow: "auto", maxHeight: 260 }}>{JSON.stringify(sel.payload, null, 2)}</pre>}
        </aside>)}
    </div>
  );
}

