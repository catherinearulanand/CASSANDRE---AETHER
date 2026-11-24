// Thin typed API client. Server errors use the single envelope {code,message,field_errors,correlation_id,retryable}.
// Money is ALWAYS a decimal string here; never convert to JS number for arithmetic.
export interface ApiErrorBody { code: string; message: string; field_errors: Record<string, string>; correlation_id: string; retryable: boolean }
export class ApiError extends Error {
  constructor(public status: number, public body: ApiErrorBody) { super(body.message); }
}
export interface Money { amount: string; currency: string }
export interface Principal {
  user_id: string; tenant_id: string; username: string; display_name: string; roles: string[];
  entitlements: string[]; csrf_token: string; data_mode: string; source_mode: string;
}
export interface Capabilities {
  data_mode: string; source_mode: string; external_transmission: string; services: string[];
  engines: { key: string; name: string; mounted: boolean; status: string }[];
}
export interface Run {
  run_id: string; scenario_id: string; market: string; mode: string; as_of: string; state: string;
  block_reasons: string[]; correlation_id: string; revision: number; created_at: string;
}

let csrf = "";
export function setCsrf(t: string) { csrf = t; }

export async function api<T>(path: string, init: { method?: string; body?: unknown; idempotencyKey?: string; signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (init.body !== undefined) headers["content-type"] = "application/json";
  if (init.method && init.method !== "GET") headers["x-csrf-token"] = csrf;
  if (init.idempotencyKey) headers["Idempotency-Key"] = init.idempotencyKey;
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, { method: init.method ?? "GET", credentials: "include", headers, signal: init.signal,
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined });
  } catch {
    throw new ApiError(0, { code: "OFFLINE", message: "Cannot reach the API (offline or server down)", field_errors: {}, correlation_id: "n/a", retryable: true });
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, data ?? { code: `HTTP_${res.status}`, message: res.statusText, field_errors: {}, correlation_id: "n/a", retryable: false });
  return data as T;
}

export const newKey = () => crypto.randomUUID();
