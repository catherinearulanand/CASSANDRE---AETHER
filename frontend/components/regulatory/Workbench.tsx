"use client";
import { useMemo, useState, type FormEvent } from "react";
import { api, ApiError, newKey } from "@/lib/api";
import { useSession } from "@/lib/session";
import { EmptyState, ErrorState, ProvenanceBadge, StatusChip } from "@/components/ui";
import { Candidate, Json, Source, Span, useLoad, fmt, short } from "./shared";

const AUTH = ["SYNTHETIC", "INTERNAL_POLICY", "INDUSTRY_STANDARD", "AUTHORITATIVE", "DRAFT_CONSULTATION", "SUPERSEDED"];
const ISSUE_TONE = (i: string): "danger" | "warn" => (/QUOTE_NOT_FOUND|AMBIGUOUS_QUOTE|CONTRADICTION|UNRESOLVED/.test(i) ? "danger" : "warn");

/** Text with page separation and span highlights (offsets are character offsets into the stored text). */
function Viewer({ src, spans, active, onPick }: { src: Source; spans: Span[]; active: string | null; onPick: (id: string) => void }) {
  const pages = useMemo(() => {
    const text = src.text ?? ""; const out: { n: number; start: number; body: string }[] = []; let off = 0;
    text.split("\f").forEach((body, i) => { out.push({ n: i + 1, start: off, body }); off += body.length + 1; });
    return out;
  }, [src.text]);
  return (
    <div role="region" aria-label="Source document viewer" tabIndex={0} style={{ maxHeight: 460, overflow: "auto", border: "1px solid var(--border)", borderRadius: 8, background: "var(--bg)" }}>
      {pages.map(p => {
        const end = p.start + p.body.length;
        const inPage = spans.filter(s => s.start < end && s.end > p.start).sort((a, b) => a.start - b.start);
        const parts: React.ReactNode[] = []; let cur = p.start;
        for (const s of inPage) {
          const a = Math.max(s.start, cur), b = Math.min(s.end, end);
          if (b <= a) continue;
          if (a > cur) parts.push(p.body.slice(cur - p.start, a - p.start));
          parts.push(<mark key={s.span_id} id={`span-${s.span_id}`} onClick={() => onPick(s.span_id)} tabIndex={0} onKeyDown={e => { if (e.key === "Enter") onPick(s.span_id); }}
            style={{ background: active === s.span_id ? "rgba(240,180,84,.45)" : "rgba(67,200,181,.3)", color: "inherit", cursor: "pointer", outline: active === s.span_id ? "2px solid var(--warn)" : "none" }}
            title={`span ${s.span_id.slice(0, 8)} · ${s.method}`}>{p.body.slice(a - p.start, b - p.start)}</mark>);
          cur = b;
        }
        if (cur < end) parts.push(p.body.slice(cur - p.start));
        return (
          <section key={p.n} aria-label={`Page ${p.n}`} style={{ padding: 12, borderBottom: "1px dashed var(--border)" }}>
            <div className="mono faint" style={{ marginBottom: 6 }}>Page {p.n} of {pages.length}{src.unreadable_pages.includes(p.n) ? " · UNREADABLE (no text layer)" : ""}</div>
            <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 13 }}>{parts.length ? parts : p.body}</pre>
          </section>
        );
      })}
    </div>
  );
}

function templateFor(c: Candidate) {
  const f = c.fields as { comparator?: string; value?: string; unit?: string; metric?: string; scope?: Record<string, string>; deadline?: string | null };
  const sc = f.scope ?? {};
  const all: unknown[] = [];
  if (sc.market_type) all.push({ eq: ["market_type", sc.market_type] });
  if (sc.venue) all.push({ eq: ["venue", sc.venue] });
  const scope = all.length ? { all } : { eq: ["market_type", "ETD_DOMESTIC"] };
  let condition: unknown = { exists: "required_margin_inr" }; let parameters: Record<string, { value: string; unit: string }> = {};
  const unit = f.unit === "PERCENT" ? "RATIO" : f.unit;
  const value = f.unit === "PERCENT" && f.value ? String(Number(f.value) / 100) : f.value ?? "0";
  if (unit === "LOTS") { condition = { lte: ["net_position_lots", { param: "limit" }] }; parameters = { limit: { value, unit: "LOTS" } }; }
  else if (f.unit === "PERCENT") { condition = { gte: ["eligible_collateral_inr", "required_margin_inr"] }; parameters = { min_coverage_ratio: { value, unit: "RATIO" } }; }
  else if (unit === "RATIO") parameters = { margin_multiplier: { value, unit: "RATIO" } };
  return { scope, condition, parameters, effective_from: f.deadline ? `${f.deadline}T00:00:00Z` : "2026-01-01T00:00:00Z", metric: f.metric ?? "rule" };
}

function Draft({ candidate, spanIds, onCreated }: { candidate: Candidate | null; spanIds: string[]; onCreated: (id: string) => void }) {
  const t = candidate ? templateFor(candidate) : null;
  const [key, setKey] = useState(""); const [scope, setScope] = useState(""); const [cond, setCond] = useState(""); const [params, setParams] = useState("");
  const [eff, setEff] = useState(""); const [vec, setVec] = useState("[]"); const [why, setWhy] = useState("");
  const [seed, setSeed] = useState<string | null>(null);
  if (candidate && seed !== candidate.candidate_id && t) {
    setSeed(candidate.candidate_id); setKey(`DEMO-${t.metric.toUpperCase().replace(/[^A-Z0-9]+/g, "-")}`); setScope(JSON.stringify(t.scope, null, 1));
    setCond(JSON.stringify(t.condition, null, 1)); setParams(JSON.stringify(t.parameters, null, 1)); setEff(t.effective_from);
  }
  const [out, setOut] = useState<{ ok: boolean; errors: { path: string; message: string }[]; tests: { name: string; passed: boolean }[] } | null>(null);
  const [err, setErr] = useState<ApiError | string | null>(null); const [busy, setBusy] = useState(false);
  const parse = (s: string) => JSON.parse(s);
  async function dry() {
    setErr(null);
    try {
      const r = await api<{ compile: { ok: boolean; errors: { path: string; message: string }[] }; tests: { name: string; passed: boolean }[] }>("/regulatory/compile",
        { method: "POST", body: { scope: parse(scope), condition: parse(cond), parameters: parse(params), test_vectors: parse(vec) } });
      setOut({ ok: r.compile.ok, errors: r.compile.errors, tests: r.tests });
    } catch (e) { setErr(e instanceof SyntaxError ? `Invalid JSON: ${e.message}` : (e as ApiError)); }
  }
  async function create(e: FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    try {
      const r = await api<{ rule_id: string }>("/regulatory/rules", { method: "POST", idempotencyKey: newKey(), body: {
        rule_key: key, authority_type: "SYNTHETIC_POLICY", scope: parse(scope), condition: parse(cond), parameters: parse(params), span_ids: spanIds,
        effective_from: eff, rationale: why, test_vectors: parse(vec), candidate_id: candidate?.candidate_id } });
      onCreated(r.rule_id);
    } catch (x) { setErr(x instanceof SyntaxError ? `Invalid JSON: ${x.message}` : (x as ApiError)); } finally { setBusy(false); }
  }
  return (
    <form className="card" onSubmit={create} aria-label="Draft rule">
      <h2>Draft rule (maker)</h2>
      <p className="muted" style={{ marginTop: 0 }}>{candidate ? "Suggested template from the selected candidate: edit and verify every field. Nothing here is activated by extraction." : "Select a candidate or write a rule by hand. Cite at least one grounded span."}</p>
      <div className="grid c2">
        <div><label htmlFor="rk">Rule key</label><input id="rk" value={key} onChange={e => setKey(e.target.value)} required /></div>
        <div><label htmlFor="ef">Effective from (UTC ISO)</label><input id="ef" value={eff} onChange={e => setEff(e.target.value)} required placeholder="2027-01-01T00:00:00Z" /></div>
      </div>
      <div className="grid c2" style={{ marginTop: 8 }}>
        <div><label htmlFor="sc">Scope (DSL JSON)</label><textarea id="sc" rows={5} className="mono" value={scope} onChange={e => setScope(e.target.value)} style={{ padding: 8 }} /></div>
        <div><label htmlFor="co">Condition (DSL JSON)</label><textarea id="co" rows={5} className="mono" value={cond} onChange={e => setCond(e.target.value)} style={{ padding: 8 }} /></div>
        <div><label htmlFor="pa">Parameters (value + unit)</label><textarea id="pa" rows={4} className="mono" value={params} onChange={e => setParams(e.target.value)} style={{ padding: 8 }} /></div>
        <div><label htmlFor="tv">Test vectors (JSON array)</label><textarea id="tv" rows={4} className="mono" value={vec} onChange={e => setVec(e.target.value)} style={{ padding: 8 }} /></div>
      </div>
      <label htmlFor="why" style={{ marginTop: 8 }}>Rationale (mandatory)</label>
      <textarea id="why" rows={2} value={why} onChange={e => setWhy(e.target.value)} required style={{ padding: 8 }} />
      <div className="faint" style={{ marginTop: 6 }}>Cited spans: {spanIds.length ? spanIds.map(s => s.slice(0, 8)).join(", ") : "none selected"}</div>
      {err && (typeof err === "string" ? <div className="field-error" role="alert">{err}</div> : <ErrorState error={err} />)}
      {out && <div role="status" style={{ marginTop: 8 }}>{out.ok ? <StatusChip tone="ok">compiles</StatusChip> : <StatusChip tone="danger">does not compile</StatusChip>}
        {out.errors.map(e => <div key={e.path} className="field-error">{e.path}: {e.message}</div>)}
        {out.tests.map(x => <div key={x.name}>{x.passed ? "✓" : "✕"} {x.name}</div>)}</div>}
      <div className="row" style={{ marginTop: 12 }}>
        <button type="button" onClick={dry}>Compile + run vectors</button>
        <button className="primary" disabled={busy || !key || !why.trim() || spanIds.length === 0}>{busy ? "Saving…" : "Save draft"}</button>
        {spanIds.length === 0 && <span className="faint">Select a grounded candidate or create a span first</span>}
      </div>
    </form>
  );
}

export default function Workbench({ initialDoc, initialSpan, openRule }: { initialDoc?: string | null; initialSpan?: string | null; openRule: (id: string) => void }) {
  const { principal } = useSession();
  const canMake = principal?.roles.includes("maker");
  const docs = useLoad(() => api<{ items: Source[] }>("/regulatory/documents"), []);
  const [sel, setSel] = useState<string | null>(initialDoc ?? null);
  const src = useLoad(() => (sel ? api<Source>(`/regulatory/documents/${sel}`) : Promise.resolve(null)), [sel]);
  const spans = useLoad(() => (sel ? api<{ items: Span[] }>(`/regulatory/documents/${sel}/spans`) : Promise.resolve(null)), [sel]);
  const cands = useLoad(() => (sel ? api<{ items: Candidate[] }>(`/regulatory/documents/${sel}/candidates`) : Promise.resolve(null)), [sel]);
  const [active, setActive] = useState<string | null>(initialSpan ?? null);
  const [cand, setCand] = useState<Candidate | null>(null);
  const [mode, setMode] = useState("REPLAY"); const [busy, setBusy] = useState(false); const [err, setErr] = useState<ApiError | null>(null);
  const [quote, setQuote] = useState(""); const [msg, setMsg] = useState<string | null>(null);

  async function upload(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); setErr(null); setMsg(null); setBusy(true);
    const form = e.currentTarget; const fd = new FormData(form);
    try {
      const res = await fetch("/api/v1/regulatory/documents", { method: "POST", body: fd, credentials: "include", headers: { "x-csrf-token": principal!.csrf_token, "Idempotency-Key": newKey() } });
      const j = await res.json();
      if (!res.ok) throw new ApiError(res.status, j);
      setMsg(j.deduplicated ? "Identical bytes already ingested; opened the existing record (deduplicated by SHA-256)." : "Ingested.");
      form.reset(); docs.reload(); setSel(j.source_id);
    } catch (x) { setErr(x as ApiError); } finally { setBusy(false); }
  }
  async function extract() {
    setBusy(true); setErr(null);
    try { await api(`/regulatory/documents/${sel}/extract`, { method: "POST", body: { mode }, idempotencyKey: newKey() }); cands.reload(); spans.reload(); }
    catch (x) { setErr(x as ApiError); } finally { setBusy(false); }
  }
  async function makeSpan() {
    setErr(null); const text = src.data?.text ?? ""; const n = quote ? text.split(quote).length - 1 : 0;
    if (n !== 1) { setErr(new ApiError(422, { code: "QUOTE_NOT_UNIQUE", message: n === 0 ? "Quote not found verbatim in the stored text." : `Quote occurs ${n} times; extend it until it is unique.`, field_errors: {}, correlation_id: "client", retryable: false })); return; }
    const start = text.indexOf(quote);
    try { const s = await api<Span>(`/regulatory/documents/${sel}/spans`, { method: "POST", body: { start, end: start + quote.length } }); spans.reload(); setActive(s.span_id); setQuote(""); }
    catch (x) { setErr(x as ApiError); }
  }
  const candSpans = cand?.span_ids ?? [];
  const draftSpans = active && !candSpans.includes(active) ? [...candSpans, active] : candSpans.length ? candSpans : active ? [active] : [];

  return (
    <div className="grid wb-grid" style={{ alignItems: "start" }}>
      <style>{".wb-grid{grid-template-columns:minmax(0,320px) minmax(0,1fr)}@media (max-width:820px){.wb-grid{grid-template-columns:minmax(0,1fr)}}"}</style>
      <aside aria-label="Source registry">
        <div className="card">
          <h2>Sources</h2>
          {docs.err ? <ErrorState error={docs.err} /> : !docs.data ? <EmptyState title="Loading…" /> : docs.data.items.length === 0 ? <EmptyState title="No sources">Run <span className="mono">python -m app.engines.regulatory.seed</span> or upload below.</EmptyState> :
            <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>{docs.data.items.map(d => (
              <li key={d.source_id}><button onClick={() => { setSel(d.source_id); setCand(null); setActive(null); }} aria-pressed={sel === d.source_id}
                style={{ width: "100%", textAlign: "left", marginBottom: 6, borderColor: sel === d.source_id ? "var(--accent)" : undefined, height: "auto", padding: 8 }}>
                <strong>{d.title}</strong><div className="faint mono">{d.doc_number ?? "no number"} · {d.authority_type}</div>
                {d.risk_flags.length > 0 && <StatusChip tone="warn">injection patterns</StatusChip>}</button></li>))}</ul>}
        </div>
        {canMake && <form className="card" onSubmit={upload} style={{ marginTop: 12 }} aria-label="Upload source">
          <h3>Upload source (PDF / HTML / text)</h3>
          <label htmlFor="file">File</label><input id="file" name="file" type="file" required accept=".pdf,.html,.htm,.txt,application/pdf,text/html,text/plain" />
          <label htmlFor="title" style={{ marginTop: 8 }}>Title</label><input id="title" name="title" required />
          <label htmlFor="authority_type" style={{ marginTop: 8 }}>Authority class</label>
          <select id="authority_type" name="authority_type" defaultValue="SYNTHETIC">{AUTH.map(a => <option key={a}>{a}</option>)}</select>
          <label htmlFor="doc_number" style={{ marginTop: 8 }}>Document number</label><input id="doc_number" name="doc_number" />
          <p className="faint">Magic bytes, size and page count are validated; the original and extracted text are stored separately.</p>
          <button className="primary" disabled={busy}>{busy ? "Uploading…" : "Ingest"}</button>
        </form>}
        {msg && <div role="status" className="muted" style={{ marginTop: 8 }}>{msg}</div>}
      </aside>
      <div>
        {err && <ErrorState error={err} />}
        {!sel ? <EmptyState title="Select a source to open the workbench" /> : src.err ? <ErrorState error={src.err} /> : !src.data ? <EmptyState title="Loading source…" /> : (
          <>
            <div className="card">
              <div className="row"><h2 style={{ margin: 0 }}>{src.data.title}</h2><StatusChip tone={src.data.authority_type === "DRAFT_CONSULTATION" ? "warn" : "info"}>{src.data.authority_type}</StatusChip>
                <ProvenanceBadge kind={src.data.provenance === "SYNTHETIC" ? "SYNTHETIC" : "PUBLIC_SOURCE"} />
                <StatusChip tone="neutral">{src.data.verification}</StatusChip></div>
              <div className="mono faint">sha256 {short(src.data.sha256)} · {src.data.kind} · {src.data.page_count} page(s) · retrieved {fmt(src.data.retrieved_at)}</div>
              {src.data.authority_type === "DRAFT_CONSULTATION" && <div role="alert" className="field-error">Draft consultation: cannot be the basis of a binding rule.</div>}
              {src.data.unreadable_pages.length > 0 && <div role="alert" className="field-error">Unreadable pages {src.data.unreadable_pages.join(", ")}: extraction is blocked; nothing is inferred.</div>}
              {src.data.risk_flags.length > 0 && <div role="alert" className="field-error">Contains instruction-like text. It is treated strictly as data and cannot trigger any tool or action.</div>}
              <div style={{ marginTop: 8 }}>{spans.data && <Viewer src={src.data} spans={spans.data.items} active={active} onPick={id => { setActive(id); document.getElementById(`span-${id}`)?.scrollIntoView({ block: "center" }); }} />}</div>
              {canMake && <div className="row" style={{ marginTop: 12 }}>
                <label htmlFor="mode" className="sr-only">Extraction mode</label>
                <select id="mode" value={mode} onChange={e => setMode(e.target.value)} style={{ width: 240 }}>
                  <option value="REPLAY">REPLAY (hand-labelled fixture)</option><option value="DETERMINISTIC">DETERMINISTIC (pattern match)</option><option value="LIVE_MODEL">LIVE_MODEL (disabled in this build)</option></select>
                <button onClick={extract} disabled={busy}>{busy ? "Extracting…" : "Extract candidates"}</button>
                <label htmlFor="q" className="sr-only">Exact quote</label>
                <input id="q" placeholder="Paste an exact quote to create a span" value={quote} onChange={e => setQuote(e.target.value)} style={{ flex: 1, minWidth: 220 }} />
                <button onClick={makeSpan} disabled={!quote}>Create span</button></div>}
            </div>
            <div className="card" style={{ marginTop: 12 }}>
              <h2>Candidates <span className="faint" style={{ fontWeight: 400 }}>(proposals only; confidence category describes the method, not accuracy)</span></h2>
              {!cands.data ? <EmptyState title="Loading…" /> : cands.data.items.length === 0 ? <EmptyState title="No candidates yet">Choose an extraction mode and run it.</EmptyState> :
                <div className="scroll-x"><table><thead><tr><th>Quote &amp; fields</th><th>Method</th><th>Issues</th><th /></tr></thead><tbody>
                  {cands.data.items.map(c => { const f = c.fields as Record<string, unknown>; return (
                    <tr key={c.candidate_id} style={{ background: cand?.candidate_id === c.candidate_id ? "var(--accent-wash)" : undefined }}>
                      <td><div className="mono" style={{ fontSize: 12 }}>{f.obligated_actor as string} · {f.metric as string} · {f.comparator as string} <strong>{String(f.value)}</strong> {String(f.unit ?? f.unit_as_written ?? "?")}{f.deadline ? ` · from ${f.deadline}` : ""}</div>
                        {c.span_ids.length ? <button style={{ minHeight: 28, padding: "0 8px", marginTop: 4 }} onClick={() => { setActive(c.span_ids[0]); document.getElementById(`span-${c.span_ids[0]}`)?.scrollIntoView({ block: "center" }); }}>Show in source</button>
                          : <StatusChip tone="danger">UNGROUNDED: quote not in source</StatusChip>}</td>
                      <td><StatusChip tone="neutral">{c.mode}</StatusChip><div className="faint">{c.confidence_category}</div></td>
                      <td>{c.issues.length ? c.issues.map(i => <div key={i}><StatusChip tone={ISSUE_TONE(i)}>{i}</StatusChip></div>) : <span className="faint">none</span>}</td>
                      <td><button disabled={!c.grounded || !canMake} title={c.grounded ? "" : "Ungrounded candidates cannot become rules"} onClick={() => { setCand(c); setActive(c.span_ids[0]); }}>Use for draft</button></td></tr>); })}
                </tbody></table></div>}
            </div>
            {canMake && <div style={{ marginTop: 12 }}><Draft candidate={cand} spanIds={draftSpans} onCreated={openRule} /></div>}
            {!canMake && <div className="faint" style={{ marginTop: 12 }}>Read-only: the maker role is required to extract or draft.</div>}
            {active && spans.data && <div className="card" style={{ marginTop: 12 }}><h3>Selected span</h3><Json v={spans.data.items.find(s => s.span_id === active) ?? null} /></div>}
          </>)}
      </div>
    </div>
  );
}
