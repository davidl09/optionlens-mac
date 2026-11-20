// JSON over HTTP to the local engine with the per-launch Bearer token (main process side).
import type { EngineConnection } from "../shared/api";

export class EngineHttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); this.name = "EngineHttpError"; }
}

export interface EngineHttp {
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body: unknown): Promise<T>;
}

type Fetch = (url: string, init: RequestInit) => Promise<Response>;

export function createEngineHttp(connection: () => EngineConnection | null, fetchFn: Fetch = fetch, timeoutMs = 60_000): EngineHttp {
  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const c = connection();
    if (!c) throw new EngineHttpError(0, "engine not running");
    let res: Response;
    try {
      res = await fetchFn(`${c.baseUrl}${path}`, {
        method,
        headers: { Authorization: `Bearer ${c.token}`, Accept: "application/json", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (e) {
      throw new EngineHttpError(0, `engine unreachable: ${(e as Error).message}`);
    }
    const text = await res.text();
    if (!res.ok) throw new EngineHttpError(res.status, `${method} ${path} → ${res.status} ${text.slice(0, 300)}`);
    return (text ? JSON.parse(text) : null) as T;
  }
  return { get: (p) => call("GET", p), post: (p, b) => call("POST", p, b) };
}
