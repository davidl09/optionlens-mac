// Tools group: seasonality, events.
import { useMemo } from "react";
import { api } from "../../api/client";
import type { ColumnDef, Events, ReportRow, Seasonality } from "../../api/types";
import { useApi } from "../../api/useApi";
import { BarChart, Heatmap } from "../../components/charts/Charts";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { Async, Card } from "../../components/ui";
import { MONTHS, WEEKDAYS } from "../../lib/format";
import { useSym } from "./shared";

const MONTH_COLUMNS: ColumnDef[] = [
  { key: "name", label: "Month", fmt: "text" },
  { key: "avg", label: "Avg return", fmt: "pct_signed" },
  { key: "median", label: "Median", fmt: "pct_signed" },
  { key: "pos_pct", label: "% positive", fmt: "pct" },
  { key: "n", label: "Years", fmt: "int" },
];

function SeasonalityBody({ s }: { s: Seasonality }) {
  const heat = useMemo(() => {
    const years = [...s.years].sort((a, b) => b - a);
    const yi = new Map(years.map((y, i) => [y, i]));
    return { years: years.map(String), cells: s.monthly.filter((m) => yi.has(m.year)).map((m) => ({ x: m.month - 1, y: yi.get(m.year) as number, value: m.ret })) };
  }, [s]);
  const cur = s.by_month.find((m) => m.month === s.current_month);
  return (
    <div className="stack">
      {cur && (
        <Card title={`This month: ${MONTHS[s.current_month - 1]}`}>
          <StatGrid cols={4} items={[
            { label: "Avg return", value: cur.avg, fmt: "pct_signed" }, { label: "Median", value: cur.median, fmt: "pct_signed" },
            { label: "% positive", value: cur.pos_pct, fmt: "pct" }, { label: "Years", value: cur.n, fmt: "int" },
          ]} />
        </Card>
      )}
      <Card title="Monthly returns by year">
        <Heatmap xLabels={MONTHS} yLabels={heat.years} cells={heat.cells} height={Math.max(220, 34 + heat.years.length * 28)} ariaLabel="Monthly return heatmap" />
      </Card>
      <div className="grid-2">
        <Card title="Average return by month">
          <BarChart categories={MONTHS} yFmt="pct_signed" signColors height={240} series={[{ name: "Avg return", data: s.by_month.map((m) => m.avg) }]} />
          <DataTable minimal rowLink={false} columns={MONTH_COLUMNS} rows={s.by_month.map((m) => ({ ...m, name: MONTHS[m.month - 1] })) as unknown as ReportRow[]} />
        </Card>
        <Card title="Day of week">
          <BarChart categories={WEEKDAYS} yFmt="pct_signed" signColors height={240} series={[{ name: "Avg return", data: s.by_weekday.map((d) => d.avg) }]} />
          <DataTable minimal rowLink={false} columns={[{ key: "name", label: "Day", fmt: "text" }, { key: "avg", label: "Avg return", fmt: "pct_signed" }, { key: "pos_pct", label: "% positive", fmt: "pct" }, { key: "n", label: "Days", fmt: "int" }]}
            rows={s.by_weekday.map((d) => ({ ...d, name: WEEKDAYS[d.weekday] ?? String(d.weekday) })) as unknown as ReportRow[]} />
        </Card>
      </div>
    </div>
  );
}

export function SeasonalityTab() {
  const sym = useSym();
  const st = useApi(() => api.seasonality(sym), [sym]);
  // types.ts has no intraday/overnight in Seasonality; that split comes from price-action.
  const pa = useApi(() => api.priceAction(sym, "1y"), [sym]);
  return (
    <div className="stack">
      <Async state={st}>{(s) => <SeasonalityBody s={s} />}</Async>
      <Async state={pa}>{(p) => (
        <Card title="Intraday vs overnight (1 year)" subtitle="overnight = open ÷ prior close; intraday = close ÷ open">
          <StatGrid cols={6} items={[
            { label: "Avg overnight", value: p.intraday_overnight.avg_overnight, fmt: "pct_signed" },
            { label: "Avg intraday", value: p.intraday_overnight.avg_intraday, fmt: "pct_signed" },
            { label: "Cum. overnight", value: p.intraday_overnight.cum_overnight, fmt: "pct_signed" },
            { label: "Cum. intraday", value: p.intraday_overnight.cum_intraday, fmt: "pct_signed" },
            { label: "Overnight up", value: p.intraday_overnight.overnight_up_pct, fmt: "pct" },
            { label: "Intraday up", value: p.intraday_overnight.intraday_up_pct, fmt: "pct" },
          ]} />
        </Card>
      )}</Async>
    </div>
  );
}

const KIND_LABEL: Record<Events["items"][number]["kind"], string> = { earnings: "Earnings", ex_dividend: "Ex-dividend", expiry: "Expiry" };

function Timeline({ items }: { items: Events["items"] }) {
  if (!items.length) return <div className="muted small">Nothing to show.</div>;
  return (
    <ol className="timeline">
      {items.map((it, i) => (
        <li key={`${it.date}-${it.kind}-${i}`} className={`tl-${it.kind}`}>
          <span className="tl-date">{it.date}</span>
          <span className={`badge tl-badge ${it.kind}`}>{KIND_LABEL[it.kind]}</span>
          <span className="tl-label">{it.label}</span>
        </li>
      ))}
    </ol>
  );
}

export function EventsTab() {
  const sym = useSym();
  const st = useApi(() => api.events(sym), [sym]);
  return (
    <Async state={st}>{(e) => {
      const upcoming = e.items.filter((x) => !x.past).sort((a, b) => a.date.localeCompare(b.date));
      const past = e.items.filter((x) => x.past).sort((a, b) => b.date.localeCompare(a.date));
      return (
        <div className="grid-2">
          <Card title="Upcoming"><Timeline items={upcoming} /></Card>
          <Card title="Past"><Timeline items={past.slice(0, 40)} /></Card>
        </div>
      );
    }}</Async>
  );
}
