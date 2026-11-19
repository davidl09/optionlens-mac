import { ReactNode } from "react";
import type { ColumnFmt } from "../api/types";
import { fmt, isMissing, signClass } from "../lib/format";
import { NullValue } from "./ui";

export interface StatItem {
  label: string;
  value?: unknown;
  fmt?: ColumnFmt;
  /** Tooltip shown when the value is missing. */
  hint?: string;
  /** Colour by sign (pct_signed is coloured automatically). */
  signed?: boolean;
  /** Custom content instead of a formatted value. */
  node?: ReactNode;
  sub?: ReactNode;
  title?: string;
}

export function StatValue({ value, kind = "num", hint, signed }: { value: unknown; kind?: ColumnFmt; hint?: string; signed?: boolean }) {
  if (isMissing(value)) return <NullValue hint={hint} />;
  const cls = signed || kind === "pct_signed" ? signClass(value) : "";
  return <span className={`${cls} ${kind === "date" || kind === "datetime" ? "nowrap" : ""}`}>{fmt(value, kind)}</span>;
}

export function StatGrid({ items, cols }: { items: StatItem[]; cols?: number }) {
  return (
    <dl className="stat-grid" style={cols ? { gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` } : undefined}>
      {items.map((it) => (
        <div className="stat" key={it.label} title={it.title}>
          <dt>{it.label}</dt>
          <dd>{it.node ?? <StatValue value={it.value} kind={it.fmt} hint={it.hint} signed={it.signed} />}{it.sub && <small>{it.sub}</small>}</dd>
        </div>
      ))}
    </dl>
  );
}
