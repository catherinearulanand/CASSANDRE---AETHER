"use client";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { EmptyState, ErrorState, StatusChip } from "@/components/ui";
import { fmt, Rule, RuleStatus, short, useLoad } from "./shared";

interface Diff {
  from: Rule; to: Rule;
  diff: { added: { path: string; value: string }[]; removed: { path: string; value: string }[]; modified: { path: string; from: string | null; to: string | null }[]; affected_fields: string[]; identical: boolean; source_spans: { from: string[]; to: string[] } };
}

/** Version timeline (effective date vs recorded date) and side-by-side diff of two revisions of one rule key. */
export default function Versions({ initialRule, openRule }: { initialRule?: string | null; openRule: (id: string) => void }) {
  const all = useLoad(() => api<{ items: Rule[] }>("/regulatory/rules?limit=500"), []);
  const [key, setKey] = useState<string>("");
  const keys = useMemo(() => [...new Set(all.data?.items.map(r => r.rule_key) ?? [])].sort(), [all.data]);
  useEffect(() => {
    if (!all.data) return;
    const init = initialRule ? all.data.items.find(r => r.rule_id === initialRule)?.rule_key : undefined;
    setKey(k => k || init || keys[0] || "");
  }, [all.data, keys, initialRule]);
  const versions = useMemo(() => (all.data?.items ?? []).filter(r => r.rule_key === key).sort((a, b) => a.version - b.version), [all.data, key]);
  const [pair, setPair] = useState<[string | null, string | null]>([null, null]);
  useEffect(() => { setPair(versions.length >= 2 ? [versions[versions.length - 2].rule_id, versions[versions.length - 1].rule_id] : [null, null]); }, [key, all.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const d = useLoad(() => (pair[0] && pair[1] ? api<Diff>(`/regulatory/rules/${pair[1]}/diff?against=${pair[0]}`) : Promise.resolve(null)), [pair[0], pair[1]]);
  const toggle = (id: string) => setPair(([a, b]) => (a === id ? [b, null] : b === id ? [a, null] : a && b ? [b, id] : a ? [a, id] : [id, null]));

  if (all.err) return <ErrorState error={all.err} />;
  if (!all.data) return <EmptyState title="Loading rules…" />;
  if (keys.length === 0) return <EmptyState title="No rules yet" />;
  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <label htmlFor="rk">Rule key</label><select id="rk" value={key} onChange={e => setKey(e.target.value)} style={{ width: 280 }}>{keys.map(k => <option key={k}>{k}</option>)}</select>
        <span className="faint">Effective date is business time; recorded date is when AETHER learned it (bitemporal).</span>
      </div>
      <section className="card" aria-labelledby="tl"><h2 id="tl">Timeline</h2>
        <div className="scroll-x"><table><thead><tr><th>Compare</th><th>Version</th><th>Status</th><th>Effective from (business)</th><th>Recorded / activated</th><th>Supersedes</th><th>Policy tests</th><th>Hash</th></tr></thead><tbody>
          {versions.map(v => <tr key={v.rule_id}>
            <td><input type="checkbox" aria-label={`Compare version ${v.version}`} checked={pair.includes(v.rule_id)} onChange={() => toggle(v.rule_id)} style={{ width: 20, minHeight: 20 }} /></td>
            <td><button style={{ minHeight: 32 }} onClick={() => openRule(v.rule_id)}>v{v.version}</button></td>
            <td><RuleStatus s={v.effective_state} />{v.superseded_by_rule_id && <div className="faint">superseded by v{versions.find(x => x.rule_id === v.superseded_by_rule_id)?.version ?? "?"}</div>}</td>
            <td className="mono">{fmt(v.effective_from)}</td><td className="mono">{fmt(v.activated_at ?? v.recorded_at)}</td>
            <td>{v.supersedes_rule_id ? `v${versions.find(x => x.rule_id === v.supersedes_rule_id)?.version ?? "?"}` : "—"}</td>
            <td>{v.compile_ok ? <StatusChip tone="ok">compiles</StatusChip> : <StatusChip tone="danger">fails</StatusChip>}</td><td className="mono">{short(v.content_hash)}</td></tr>)}
        </tbody></table></div>
      </section>
      <section className="card" style={{ marginTop: 16 }} aria-labelledby="df"><h2 id="df">Difference report</h2>
        {!pair[0] || !pair[1] ? <EmptyState title="Select two versions to compare" /> : d.err ? <ErrorState error={d.err} /> : !d.data ? <EmptyState title="Computing…" /> : (
          <>
            <div className="grid c2">
              {[d.data.from, d.data.to].map((r, i) => <div key={r.rule_id} className="card" style={{ background: "var(--bg)" }}>
                <h3>{i === 0 ? "Older" : "Newer"}: v{r.version}</h3><RuleStatus s={r.effective_state} />
                <div className="mono faint">effective {fmt(r.effective_from)} · recorded {fmt(r.recorded_at)}</div>
                <div className="mono faint">spans {r.span_ids.map(s => s.slice(0, 8)).join(", ") || "none"}</div></div>)}
            </div>
            {d.data.diff.identical ? <p>No content differences.</p> : (
              <div className="scroll-x"><table style={{ marginTop: 12 }}><thead><tr><th>Change</th><th>Path</th><th>Before</th><th>After</th></tr></thead><tbody>
                {d.data.diff.modified.map(m => <tr key={m.path}><td><StatusChip tone="warn">modified</StatusChip></td><td className="mono">{m.path}</td><td className="mono">{m.from ?? "—"}</td><td className="mono">{m.to ?? "—"}</td></tr>)}
                {d.data.diff.added.map(m => <tr key={m.path}><td><StatusChip tone="ok">added</StatusChip></td><td className="mono">{m.path}</td><td>—</td><td className="mono">{m.value}</td></tr>)}
                {d.data.diff.removed.map(m => <tr key={m.path}><td><StatusChip tone="danger">removed</StatusChip></td><td className="mono">{m.path}</td><td className="mono">{m.value}</td><td>—</td></tr>)}
              </tbody></table></div>)}
            <p className="muted">Affected fields: <span className="mono">{d.data.diff.affected_fields.join(", ") || "—"}</span></p>
          </>)}
      </section>
    </>
  );
}
