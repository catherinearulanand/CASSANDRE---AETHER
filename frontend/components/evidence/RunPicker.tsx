"use client";
import { api, type Run } from "@/lib/api";
import { EmptyState, ErrorState } from "@/components/ui";
import { useLoad } from "./useLoad";

export default function RunPicker({ value, onChange, label = "Run" }: { value: string; onChange: (id: string) => void; label?: string }) {
  const { data, error, loading } = useLoad<{ items: Run[] }>(s => api("/runs?limit=100", { signal: s }), []);
  if (error) return <ErrorState error={error} />;
  if (loading) return <EmptyState title="Loading runs…" />;
  if (!data || data.items.length === 0) return <EmptyState title="No runs">Create a run in the Scenario Lab first.</EmptyState>;
  return (
    <div style={{ minWidth: 260 }}>
      <label htmlFor="run-picker">{label}</label>
      <select id="run-picker" value={value} onChange={e => onChange(e.target.value)}>
        <option value="">Select a run…</option>
        {data.items.map(r => <option key={r.run_id} value={r.run_id}>{r.run_id.slice(0, 8)} · {r.scenario_id} · {r.state}</option>)}
      </select>
    </div>
  );
}
