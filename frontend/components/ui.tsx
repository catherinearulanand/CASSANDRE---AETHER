"use client";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";

type Tone = "ok" | "warn" | "danger" | "info" | "neutral";
const ICON: Record<Tone, string> = { ok: "✓", warn: "!", danger: "✕", info: "i", neutral: "•" };
/** Status = icon + text + colour (never colour alone). */
export function StatusChip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`chip ${tone}`}><span aria-hidden>{ICON[tone]}</span>{children}</span>;
}
export function ProvenanceBadge({ kind }: { kind: "SYNTHETIC" | "REPLAY" | "LIVE_MODEL" | "MANUAL" | "HYPOTHETICAL" | "PUBLIC_SOURCE" }) {
  const tone: Tone = kind === "HYPOTHETICAL" ? "warn" : kind === "LIVE_MODEL" ? "info" : "neutral";
  return <StatusChip tone={tone}>{kind}</StatusChip>;
}
export function MetricCard({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return <div className="card metric"><div className="label">{label}</div><div className="value">{value}</div>{sub && <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>{sub}</div>}</div>;
}
/** Exact amount with explicit sign and currency. Decimal STRING in, string out (no float conversion). */
export function MoneyDisplay({ amount, currency, signed = true }: { amount: string; currency: string; signed?: boolean }) {
  const neg = amount.startsWith("-"); const abs = neg ? amount.slice(1) : amount;
  const [i, f] = abs.split("."); const grouped = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const sign = neg ? "−" : signed && abs !== "0" ? "+" : "";
  return <span className="num" title={`${amount} ${currency}`}>{sign}{grouped}{f ? "." + f : ""} {currency}{neg ? " (negative)" : ""}</span>;
}
export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="state" role="status"><strong>{title}</strong>{children && <div style={{ marginTop: 6 }}>{children}</div>}</div>;
}
export function ErrorState({ error }: { error: ApiError | Error }) {
  const e = error instanceof ApiError ? error.body : null;
  return <div className="state error" role="alert"><strong>{e?.code ?? "ERROR"}</strong>: {error.message}
    {e && <div className="mono faint" style={{ marginTop: 6 }}>correlation_id {e.correlation_id}{e.retryable ? " · retryable" : ""}</div>}</div>;
}
export function NotImplemented({ engine, items }: { engine: string; items: string[] }) {
  return <div className="card"><StatusChip tone="warn">NOT IMPLEMENTED IN THIS BUILD</StatusChip>
    <p className="muted">The {engine} engine is not mounted on the API, so no screens or actions are offered. Nothing below is simulated.</p>
    <ul className="muted">{items.map(i => <li key={i}>{i}</li>)}</ul></div>;
}
