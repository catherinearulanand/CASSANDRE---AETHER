"use client";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, setCsrf, type Capabilities, type Principal } from "./api";

export type Market = "OTC" | "ETD_DOMESTIC" | "ETD_IFSC";
interface Ctx {
  principal: Principal | null; caps: Capabilities | null; loading: boolean; error: ApiError | null;
  market: Market; setMarket: (m: Market) => void; asOf: string;
  signIn: (tenant: string, username: string, password: string) => Promise<void>; signOut: () => Promise<void>;
}
const SessionCtx = createContext<Ctx | null>(null);
export const useSession = () => { const c = useContext(SessionCtx); if (!c) throw new Error("no session provider"); return c; };

// Pinned as-of for the synthetic scenarios; never the browser clock (replay mode has no implicit clock).
export const PINNED_AS_OF = "2026-08-17T04:00:00Z";

export function SessionProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [market, setMarketState] = useState<Market>("ETD_DOMESTIC");

  const load = useCallback(async () => {
    try {
      const p = await api<Principal>("/session"); setCsrf(p.csrf_token); setPrincipal(p);
      setCaps(await api<Capabilities>("/capabilities")); setError(null);
    } catch (e) { setPrincipal(null); if (e instanceof ApiError && e.status !== 401) setError(e); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); const m = localStorage.getItem("aether.market") as Market | null; if (m) setMarketState(m); }, [load]);

  const setMarket = (m: Market) => { setMarketState(m); localStorage.setItem("aether.market", m); };
  const signIn = async (tenant_id: string, username: string, password: string) => {
    const p = await api<Principal>("/session/login", { method: "POST", body: { tenant_id, username, password } });
    setCsrf(p.csrf_token); setPrincipal(p); setCaps(await api<Capabilities>("/capabilities"));
  };
  const signOut = async () => { await api("/session/logout", { method: "POST" }); setPrincipal(null); setCaps(null); };
  return <SessionCtx.Provider value={{ principal, caps, loading, error, market, setMarket, asOf: PINNED_AS_OF, signIn, signOut }}>{children}</SessionCtx.Provider>;
}
