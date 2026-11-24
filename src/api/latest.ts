// Latest-wins request runner for inputs that change faster than the engine answers (the calculator while live
// quotes tick): at most one request in flight, starts at least `minIntervalMs` apart, and when a request ends the
// newest wanted key is sent next. Every completed answer is shown, in order, so a slow engine never starves the view
// (unlike discarding in-flight answers on each change). Pure: the React hook is useLatest in useApi.ts.

export interface LatestState<T> { data: T | null; error: Error | null; /** The key `data` answers. */ dataKey: string | null; busy: boolean }

export interface LatestDeps {
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (t: unknown) => void;
}

export class LatestRunner<T> {
  state: LatestState<T> = { data: null, error: null, dataKey: null, busy: false };
  private want: string | null = null;
  private sent: string | null = null;
  private inflight = false;
  private lastStart = -Infinity;
  private timer: unknown = null;
  private disposed = false;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (t: unknown) => void;

  constructor(private readonly run: (key: string) => Promise<T>, private readonly minIntervalMs: number,
    private readonly onChange: (s: LatestState<T>) => void, d: LatestDeps = {}) {
    this.now = d.now ?? Date.now;
    this.setTimer = d.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = d.clearTimer ?? ((t) => clearTimeout(t as ReturnType<typeof setTimeout>));
  }

  /** The key the view wants answered now (null: nothing to send; the last answer stays). */
  request(key: string | null): void {
    this.want = key;
    this.pump();
  }

  /** Send the wanted key again even if it was sent (a new engine connection / reload). */
  invalidate(): void {
    this.sent = null;
    this.pump();
  }

  dispose(): void {
    this.disposed = true;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
  }

  private set(patch: Partial<LatestState<T>>): void {
    this.state = { ...this.state, ...patch };
    this.onChange(this.state);
  }

  private pump(): void {
    const key = this.want;
    if (this.disposed || key === null || this.inflight || key === this.sent) return;
    const wait = this.lastStart + this.minIntervalMs - this.now();
    if (wait > 0) {
      if (this.timer === null) this.timer = this.setTimer(() => { this.timer = null; this.pump(); }, wait);
      return;
    }
    this.inflight = true;
    this.sent = key;
    this.lastStart = this.now();
    this.set({ busy: true });
    let p: Promise<T>;
    try { p = this.run(key); } catch (e) { p = Promise.reject(e); }
    p.then(
      (data) => { if (!this.disposed) this.set({ data, error: null, dataKey: key }); },
      (e: unknown) => { if (!this.disposed) this.set({ error: e instanceof Error ? e : new Error(String(e)) }); },
    ).finally(() => {
      this.inflight = false;
      if (this.disposed) return;
      this.set({ busy: false });
      this.pump();
    });
  }
}
