// Polls the engine's unread notifications and shows a native notification for each new one.
// "New" = id above the highest id already shown (persisted, so a restart does not repeat them).
export interface EngineNotification { id: number; title: string; body: string; link: string | null }

export interface NotifierDeps {
  fetchUnread: () => Promise<EngineNotification[]>;
  show: (n: EngineNotification) => void;
  lastShownId: () => number;
  setLastShownId: (id: number) => void;
  onError?: (e: unknown) => void;
}

/** At most this many are shown per poll (the bell lists the rest). */
export const MAX_PER_POLL = 5;

/** New notifications to show, oldest first, capped to the newest MAX_PER_POLL. */
export function newNotifications(items: EngineNotification[], lastShownId: number): EngineNotification[] {
  return items.filter((n) => n.id > lastShownId).sort((a, b) => a.id - b.id).slice(-MAX_PER_POLL);
}

export function isNotificationList(v: unknown): v is EngineNotification[] {
  return Array.isArray(v) && v.every((n) => typeof n === "object" && n !== null && typeof (n as EngineNotification).id === "number"
    && typeof (n as EngineNotification).title === "string");
}

export class NotificationPoller {
  private timer: ReturnType<typeof setInterval> | null = null;
  private busy = false;

  constructor(private readonly d: NotifierDeps, private readonly intervalMs = 60_000) {}

  start(): void {
    if (this.timer) return;
    void this.poll();
    this.timer = setInterval(() => void this.poll(), this.intervalMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async poll(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      const items = await this.d.fetchUnread();
      const fresh = newNotifications(items, this.d.lastShownId());
      for (const n of fresh) this.d.show(n);
      const max = Math.max(this.d.lastShownId(), ...items.map((n) => n.id));
      if (max > this.d.lastShownId()) this.d.setLastShownId(max);
    } catch (e) {
      this.d.onError?.(e);
    } finally {
      this.busy = false;
    }
  }
}

/** Notification link → in-app route (only relative app paths; anything else opens the alerts page). */
export function notificationRoute(link: string | null): string {
  return link && link.startsWith("/") && !link.startsWith("//") ? link : "/alerts";
}
