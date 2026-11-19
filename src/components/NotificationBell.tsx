// Header bell: polls unread notifications every 60 s.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Notification } from "../api/types";
import { fmtDateTime } from "../lib/format";
import { useClickOutside } from "./ui";

const POLL_MS = 60_000;

export function NotificationBell() {
  const [items, setItems] = useState<Notification[]>([]);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useClickOutside(ref, close, open);

  const load = useCallback(() => {
    api.notifications(true).then(setItems, () => { /* bell stays quiet when the server is down */ });
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);

  const markAll = async () => { try { await api.markNotificationsRead(); setItems([]); } catch { /* ignore */ } };
  const markOne = async (id: number) => { try { await api.markNotificationsRead([id]); setItems((xs) => xs.filter((x) => x.id !== id)); } catch { /* ignore */ } };

  return (
    <div className="pop-anchor" ref={ref}>
      <button className="icon-btn" onClick={() => setOpen((o) => !o)} aria-label={`Notifications (${items.length} unread)`} aria-expanded={open}>
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 3a6 6 0 0 0-6 6v3.6L4.3 16h15.4L18 12.6V9a6 6 0 0 0-6-6Zm-2.2 15a2.3 2.3 0 0 0 4.4 0Z" fill="currentColor" /></svg>
        {items.length > 0 && <span className="bell-count">{items.length > 99 ? "99+" : items.length}</span>}
      </button>
      {open && (
        <div className="popover right bell-pop">
          <div className="bell-head"><b>Notifications</b>{items.length > 0 && <button className="btn btn-sm btn-ghost" onClick={markAll}>Mark all read</button>}</div>
          {!items.length && <div className="muted small pad">No unread notifications.</div>}
          <ul className="bell-list">
            {items.slice(0, 12).map((n) => (
              <li key={n.id}>
                <div className="bell-title">{n.link ? <Link to={n.link} onClick={() => { void markOne(n.id); close(); }}>{n.title}</Link> : n.title}</div>
                <div className="bell-body">{n.body}</div>
                <div className="bell-meta"><span>{fmtDateTime(n.ts)}</span><button className="link-btn" onClick={() => void markOne(n.id)}>mark read</button></div>
              </li>
            ))}
          </ul>
          <div className="bell-foot"><Link to="/alerts" onClick={close}>All alerts & notifications →</Link></div>
        </div>
      )}
    </div>
  );
}
