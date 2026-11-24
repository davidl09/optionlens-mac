// /api/live (INTEGRATION.md §9) mocks: the default stream says the market is closed (no patches); realtime.spec.ts
// serves its own frames.
export const sse = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

export const STATUS = {
  live: { state: "live", source: "questrade", session: "regular", mode: "poll", interval_s: 2, symbols: 3, contracts: 42, detail: null },
  delayed: { state: "delayed", source: "questrade", session: "regular", mode: "poll", interval_s: 2, symbols: 1, contracts: 0, detail: null },
  closed: { state: "market_closed", source: null, session: "closed", mode: null, interval_s: null, symbols: 0, contracts: 0, detail: "US markets are closed: showing the last stored values. Live quotes resume Mon 04:00 ET." },
} as const;

export const defaultStream = () => sse("status", STATUS.closed);
export const subscribed = (body: { client_id?: string; symbols?: string[]; option_contracts?: string[] } | null) =>
  ({ client_id: body?.client_id ?? "", symbols: body?.symbols?.length ?? 0, contracts: body?.option_contracts?.length ?? 0, status: STATUS.closed });
