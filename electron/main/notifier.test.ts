import { EventEmitter } from "node:events";
import { describe, expect, it } from "vitest";
import { MAX_PER_POLL, newNotifications, NotificationPoller, notificationRoute, showRetained, type EngineNotification } from "./notifier";

const n = (id: number, link: string | null = null): EngineNotification => ({ id, title: `t${id}`, body: "b", link });

describe("notifications", () => {
  it("shows only ids above the last shown, capped, oldest first", () => {
    expect(newNotifications([n(3), n(1), n(2)], 1).map((x) => x.id)).toEqual([2, 3]);
    const many = Array.from({ length: 12 }, (_, i) => n(i + 1));
    expect(newNotifications(many, 0).map((x) => x.id)).toEqual([8, 9, 10, 11, 12].slice(-MAX_PER_POLL));
  });

  it("persists the high-water mark so polls (and restarts) do not repeat", async () => {
    let last = 0;
    const shown: number[] = [];
    let items = [n(1), n(2)];
    const p = new NotificationPoller({ fetchUnread: async () => items, show: (x) => shown.push(x.id), lastShownId: () => last, setLastShownId: (id) => { last = id; } });
    await p.poll();
    await p.poll();
    items = [n(2), n(5)];
    await p.poll();
    expect(shown).toEqual([1, 2, 5]);
    expect(last).toBe(5);
  });

  it("survives fetch errors", async () => {
    const errors: unknown[] = [];
    const p = new NotificationPoller({ fetchUnread: async () => { throw new Error("down"); }, show: () => undefined, lastShownId: () => 0, setLastShownId: () => undefined, onError: (e) => errors.push(e) });
    await p.poll();
    expect(errors).toHaveLength(1);
  });

  it("routes only in-app links", () => {
    expect(notificationRoute("/s/AAPL/summary")).toBe("/s/AAPL/summary");
    expect(notificationRoute("https://evil.example")).toBe("/alerts");
    expect(notificationRoute("//evil.example")).toBe("/alerts");
    expect(notificationRoute(null)).toBe("/alerts");
  });
});

describe("showRetained", () => {
  class FakeNotification extends EventEmitter { shown = false; show() { this.shown = true; } }

  it("keeps a shown notification referenced until it is clicked, closed or failed", () => {
    const live = new Set<FakeNotification>();
    const clicks: string[] = [];
    const failures: string[] = [];
    const [a, b, c] = [new FakeNotification(), new FakeNotification(), new FakeNotification()];
    for (const n of [a, b, c]) showRetained(n, live, () => clicks.push("click"), (e) => failures.push(e));
    expect(a.shown && b.shown && c.shown).toBe(true);
    expect(live.size).toBe(3);
    a.emit("click");
    b.emit("close");
    c.emit("failed", {}, "not authorized");
    expect(live.size).toBe(0);
    expect(clicks).toEqual(["click"]);
    expect(failures).toEqual(["not authorized"]);
  });
});
