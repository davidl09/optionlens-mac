// When option data is collected (INTEGRATION.md §6): shared by every empty chain-based view.
import { api } from "../api/client";
import type { Status } from "../api/types";
import { useApi } from "../api/useApi";
import { fmtDateTime } from "../lib/format";
import { COLLECTION_END_H, nextCollectionWindow, populateText } from "../lib/status";
import { useBridgeStatus } from "../state/desktop";

export const CHAIN_COLLECTION_TEXT =
  "Option chains are collected by the IBKR bridge during market hours (09:00–20:00 ET), after hours for a last session that has none yet, and on demand when you open a symbol.";

/** The next collection: the engine's scheduled sweep when it has one, else the bridge's next window. */
export function nextCollection(s: Status): string {
  if (s.next_sweep_at) return fmtDateTime(s.next_sweep_at);
  return nextCollectionWindow(new Date(s.now)) ?? `now (until ${COLLECTION_END_H}:00 ET)`;
}

/** "Last bridge push … · next collection …" from the engine's status. */
export function collectionTimes(s: Status): string {
  const last = s.bridge.last_push_at ? `Last bridge push ${fmtDateTime(s.bridge.last_push_at)}` : "The bridge has not pushed any data yet";
  return `${last} · next collection ${nextCollection(s)}.`;
}

export function CollectionTimes() {
  const st = useApi(api.status, []);
  return st.data ? <span className="collection-times">{collectionTimes(st.data)}</span> : null;
}

/** "Populating last session's data: 340/1145." while the bridge fills the last session (empty views say so), else nothing. */
export function PopulationNote() {
  const text = populateText(useBridgeStatus());
  return text ? <span className="population-note" role="status">{text}.</span> : null;
}

/** The chain-collection sentence plus the last/next collection times. */
export function ChainCollectionNote() {
  return <span className="chain-collection">{CHAIN_COLLECTION_TEXT} <CollectionTimes /></span>;
}
