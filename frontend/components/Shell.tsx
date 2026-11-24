"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { SessionProvider, useSession, type Market } from "@/lib/session";
import { ErrorState } from "./ui";

const NAV = [
  ["/overview", "Overview"], ["/regulatory", "Regulatory"], ["/contracts", "Contracts"], ["/risk", "Risk & Margin"],
  ["/collateral", "Collateral & Liquidity"], ["/compliance", "Compliance"], ["/evidence", "Evidence"],
  ["/scenarios", "Scenario Lab"], ["/methodology", "Methodology"], ["/about", "About"],
] as const;
const KEY: Record<string, string> = {
  "/regulatory": "regulatory", "/contracts": "contracts", "/risk": "risk",
  "/collateral": "collateral", "/compliance": "compliance", "/evidence": "evidence",
};
const MARKETS: [Market, string][] = [["OTC", "OTC (bilateral)"], ["ETD_DOMESTIC", "Domestic ETD · INR"], ["ETD_IFSC", "IFSC ETD · USD"]];

function Login() {
  const { signIn } = useSession();
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true); setErr(null);
    try { await signIn(String(f.get("tenant")), String(f.get("username")), String(f.get("password"))); }
    catch (x) { setErr(x as ApiError); } finally { setBusy(false); }
  }
  return (
    <main style={{ maxWidth: 380, margin: "10vh auto", padding: 16 }}>
      <div className="brand" style={{ justifyContent: "center" }}><img src="/brand/cassandre-logo.jpg" alt="Cassandré" /><span>AETHER</span></div>
      <form className="card" onSubmit={submit} aria-label="Sign in">
        <h1>Sign in</h1>
        <p className="muted">Synthetic-data workstation. Credentials are generated locally by <span className="mono">seed.py</span>.</p>
        <div style={{ marginBottom: 12 }}><label htmlFor="tenant">Tenant</label><input id="tenant" name="tenant" defaultValue="tenant-a" required /></div>
        <div style={{ marginBottom: 12 }}><label htmlFor="username">Username</label><input id="username" name="username" autoComplete="username" required /></div>
        <div style={{ marginBottom: 12 }}><label htmlFor="password">Password</label><input id="password" name="password" type="password" autoComplete="current-password" required /></div>
        {err && <ErrorState error={err} />}
        <button className="primary" disabled={busy} style={{ marginTop: 12, width: "100%" }}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>
  );
}

function Frame({ children }: { children: ReactNode }) {
  const { principal, caps, loading, error, market, setMarket, asOf, signOut } = useSession();
  const path = usePathname();
  const [open, setOpen] = useState(false);
  if (loading) return <div className="state" role="status" style={{ margin: 40 }}>Loading session…</div>;
  if (error) return <div style={{ margin: 40 }}><ErrorState error={error} /></div>;
  if (!principal) return <Login />;
  return (
    <div className="shell">
      <div className="banner" role="note">
        SYNTHETIC DATA · SIMULATION ONLY — decision support; no trade, settlement or regulatory submission is transmitted. Source mode: {principal.source_mode}.
      </div>
      <nav className={`sidebar nav${open ? " open" : ""}`} aria-label="Primary">
        <div className="brand"><img src="/brand/cassandre-logo.jpg" alt="Cassandré" /><span>AETHER</span></div>
        {NAV.map(([href, label]) => {
          const eng = KEY[href];
          const missing = eng && caps && !caps.engines.find(e => e.key === eng)?.mounted;
          const current = path === href || path.startsWith(href + "/");
          return (
            <Link key={href} href={href} onClick={() => setOpen(false)} aria-current={current ? "page" : undefined}>
              {label}{missing && <span className="tag">not built</span>}
            </Link>
          );
        })}
      </nav>
      <header className="topbar">
        <button className="menu-btn" aria-label="Toggle navigation" onClick={() => setOpen(o => !o)}>☰</button>
        <label className="sr-only" htmlFor="market">Market</label>
        <select id="market" value={market} onChange={e => setMarket(e.target.value as Market)} style={{ width: 200 }}>
          {MARKETS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <span className="mono muted asof" title="Pinned scenario as-of (not the browser clock)">as-of {asOf} UTC</span>
        <span className="spacer" />
        <span className="muted who">{principal.tenant_id} · {principal.display_name} · <span className="mono">{principal.roles.join(",")}</span></span>
        <button onClick={() => signOut()}>Sign out</button>
      </header>
      <main className="main">{children}</main>
    </div>
  );
}

export default function Shell({ children }: { children: ReactNode }) {
  return <SessionProvider><Frame>{children}</Frame></SessionProvider>;
}
