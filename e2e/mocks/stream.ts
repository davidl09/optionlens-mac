// A mocked engine whose live channel (GET /api/live/stream) is served frame by frame by the test: each stream request
// waits for the next `serve(frames)`; every other /api request is answered by the shared mocks (index.ts).
import type { Page } from "@playwright/test";
import { handle } from "./index";

export interface Live {
  /** The body of the next stream request (each stream request waits for one). */
  serve: (frames: string) => void;
  /** The window's subscriptions as posted. */
  subscriptions: { symbols: string[]; option_contracts: string[]; chains: { symbol: string; expiry: string; strike_min: number; strike_max: number }[] }[];
  streams: number;
  errors: string[];
}

export async function setup(page: Page, opts: { failStream?: boolean } = {}): Promise<Live> {
  const queue: string[] = [];
  const waiting: ((f: string) => void)[] = [];
  const live: Live = {
    subscriptions: [], streams: 0, errors: [],
    serve: (f) => { const w = waiting.shift(); if (w) w(f); else queue.push(f); },
  };
  page.on("pageerror", (e) => live.errors.push(e.message));
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.pathname === "/api/live/stream") {
      live.streams += 1;
      if (opts.failStream) { await route.fulfill({ status: 503, json: { detail: "down" } }); return; }
      const frames = queue.shift() ?? await new Promise<string>((r) => waiting.push(r));
      await route.fulfill({ status: 200, body: frames, contentType: "text/event-stream" }).catch(() => undefined);
      return;
    }
    let body: unknown = null;
    try { body = req.postDataJSON(); } catch { body = null; }
    if (url.pathname === "/api/live/subscribe") live.subscriptions.push(body as Live["subscriptions"][number]);
    const res = handle(req.method(), url, body);
    if (!res) { await route.fulfill({ status: 501, json: { detail: "not mocked" } }); return; }
    await route.fulfill(res.text !== undefined ? { status: res.status, body: res.text, contentType: res.contentType } : { status: res.status, json: res.body });
  });
  return live;
}

