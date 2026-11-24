// Reports the current view to the engine (PUT /api/view, INTEGRATION.md §10) so AI agents can see what the user is
// looking at: on navigation and whenever the page's data, table state or details change (debounced, only when it
// differs from what was last sent).
import { useEffect, useRef, useSyncExternalStore } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api/client";
import { describeRoute, viewStore, type CurrentView } from "../state/view";

export const REPORT_DEBOUNCE_MS = 300;

export function ViewReporter() {
  const loc = useLocation();
  const version = useSyncExternalStore(viewStore.subscribe, viewStore.version);
  const sent = useRef("");
  useEffect(() => {
    const t = window.setTimeout(() => {
      const { data, details } = viewStore.snapshot();
      const title = document.querySelector(".app-main h1")?.textContent?.trim() || null;
      const view: CurrentView = { ...describeRoute(loc.pathname, loc.search), title: title?.slice(0, 300) ?? null, details, data };
      const key = JSON.stringify(view);
      if (key === sent.current) return;
      sent.current = key;
      api.reportView(view).catch(() => { sent.current = ""; });   // engine restarting: sent again on the next change
    }, REPORT_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [loc.pathname, loc.search, version]);
  return null;
}
