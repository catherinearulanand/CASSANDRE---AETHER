"use client";
import type { ReactNode } from "react";
import { useSession } from "@/lib/session";
import { NotImplemented } from "./ui";

/** Renders children only when the engine is actually mounted on the API; otherwise an honest NOT IMPLEMENTED state. */
export default function EngineGate({ engine, name, planned, children }: { engine: string; name: string; planned: string[]; children?: ReactNode }) {
  const { caps } = useSession();
  const mounted = caps?.engines.find(e => e.key === engine)?.mounted;
  return <><h1>{name}</h1>{mounted ? children : <NotImplemented engine={name} items={planned} />}</>;
}
