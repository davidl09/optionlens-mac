// Volatility group: volatility, skew, implied moves, vol seasonality.
import { useMemo, useState } from "react";
import { api } from "../../api/client";
import type { ColumnDef, ReportRow, Skew, TermPoint, Volatility } from "../../api/types";
import { useApi } from "../../api/useApi";
import { BarChart, LineChart, LineSeries, Smile } from "../../components/charts/Charts";
import { usePalette } from "../../components/charts/EChart";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { AsOf, Async, Card, InfoBox, Segmented } from "../../components/ui";
import { fmt, MONTHS } from "../../lib/format";
import { ExpiryPills, shortDate, useQueryValue, useSym } from "./shared";
import { ivHistoryHint } from "./SummaryTab";

const CM_COLUMNS: ColumnDef[] = [
  { key: "tenor", label: "Tenor (days)", fmt: "int" },
  { key: "iv", label: "IV", fmt: "vol" },
  { key: "chg_1d", label: "1d chg (pts)", fmt: "vol" },
  { key: "chg_1w", label: "1w chg (pts)", fmt: "vol" },
];

function termSeries(name: string, pts: TermPoint[] | null, color: string, dashed = false): LineSeries | null {
  return pts && pts.length ? { name, data: pts.map((t) => [t.dte, t.atm_iv]), color, dashed } : null;
}

function VolatilityBody({ v }: { v: Volatility }) {
  const p = usePalette();
  const [extra, setExtra] = useState<string[]>([]);
  const hint = ivHistoryHint(v.history_days);
  const hist = useMemo<LineSeries[]>(() => {
    const s: LineSeries[] = [
      { name: "IV30", data: v.history.map((h) => [h.date, h.iv30]), color: p.series[0] },
      { name: "HV20", data: v.history.map((h) => [h.date, h.hv20]), color: p.series[1] },
    ];
    if (extra.includes("hv10")) s.push({ name: "HV10", data: v.history.map((h) => [h.date, h.hv10]), color: p.series[3] });
    if (extra.includes("hv60")) s.push({ name: "HV60", data: v.history.map((h) => [h.date, h.hv60]), color: p.series[2] });
    if (extra.includes("iv90")) s.push({ name: "IV90", data: v.history.map((h) => [h.date, h.iv90]), color: p.series[6], dashed: true });
    return s;
  }, [v.history, extra, p]);
  const term = useMemo(() => [
    termSeries("Today", v.term_today, p.series[0]), termSeries("1 week ago", v.term_1w, p.series[2], true), termSeries("1 month ago", v.term_1m, p.series[6], true),
  ].filter((x): x is LineSeries => x !== null), [v, p]);
  const premium = useMemo<LineSeries[]>(() => [
    { name: "IV30 − HV20", data: v.risk_premium.map((r) => [r.date, r.iv30 !== null && r.hv20 !== null ? r.iv30 - r.hv20 : null]), color: p.series[0], area: true },
  ], [v.risk_premium, p]);
  const realized = useMemo<LineSeries[]>(() => [
    { name: "IV30 at date", data: v.risk_premium.map((r) => [r.date, r.iv30]), color: p.series[0] },
    { name: "Realized next 21d", data: v.risk_premium.map((r) => [r.date, r.realized_fwd21]), color: p.series[1] },
  ], [v.risk_premium, p]);
  const hasRealized = v.risk_premium.some((r) => r.realized_fwd21 !== null && r.iv30 !== null);

  return (
    <div className="stack">
      <Card title="Implied volatility" subtitle={<AsOf ts={v.asof} />}>
        <StatGrid items={[
          { label: "IV30", value: v.iv30, fmt: "vol" },
          { label: "IV rank (52w)", value: v.iv_rank, fmt: "pct", hint },
          { label: "IV percentile", value: v.iv_percentile, fmt: "pct", hint },
          { label: "IV history", value: v.history_days, fmt: "int", sub: "days collected" },
          { label: "52w IV high", value: v.iv_high_52w, fmt: "vol", hint },
          { label: "52w IV low", value: v.iv_low_52w, fmt: "vol", hint },
          ...v.hv.map((h) => ({ label: `HV${h.window}`, value: h.value, fmt: "vol" as const })),
          { label: "Parkinson HV20", value: v.parkinson20, fmt: "vol" },
        ]} />
      </Card>
      <Card title="IV30 vs historical volatility" actions={
        <div className="toggle-chips">
          {[["hv10", "HV10"], ["hv60", "HV60"], ["iv90", "IV90"]].map(([k, l]) => (
            <label key={k} className={`chip ${extra.includes(k) ? "on" : ""}`}>
              <input type="checkbox" checked={extra.includes(k)} onChange={() => setExtra((e) => (e.includes(k) ? e.filter((x) => x !== k) : [...e, k]))} />{l}
            </label>
          ))}
        </div>
      } subtitle="HV goes back 10 years; IV starts when collection started">
        <LineChart series={hist} yFmt="vol" height={300} zoom />
      </Card>
      <div className="grid-2">
        <Card title="Term structure (ATM IV by DTE)" subtitle={v.term_1w ? undefined : "1w / 1m comparison appears once stored history allows"}>
          <LineChart series={term} xType="value" xFmt="int" yFmt="vol" height={260} />
        </Card>
        <Card title="Constant-maturity IV">
          <DataTable minimal rowLink={false} columns={CM_COLUMNS} rows={v.constant_maturity as unknown as ReportRow[]} />
        </Card>
      </div>
      <div className="grid-2">
        <Card title="Volatility risk premium" subtitle="IV30 minus HV20 (vol points)">
          <LineChart series={premium} yFmt="vol" height={240} />
        </Card>
        <Card title="Implied vs subsequently realized" subtitle="IV30 on a date vs HV over the next 21 trading days">
          {hasRealized ? <LineChart series={realized} yFmt="vol" height={240} /> : <InfoBox>Needs at least 21 trading days of collected IV history.</InfoBox>}
        </Card>
      </div>
    </div>
  );
}

export function VolatilityTab() {
  const sym = useSym();
  const st = useApi(() => api.volatility(sym), [sym]);
  return <Async state={st}>{(v) => <VolatilityBody v={v} />}</Async>;
}

function SkewBody({ s, setExpiry }: { s: Skew; setExpiry: (e: string) => void }) {
  const p = usePalette();
  const [mode, setMode] = useState<"strike" | "delta">("strike");
  const smile = useMemo<LineSeries[]>(() => mode === "strike"
    ? [
      { name: "Call IV", data: s.points.map((x) => [x.strike, x.call_iv]), color: p.series[0] },
      { name: "Put IV", data: s.points.map((x) => [x.strike, x.put_iv]), color: p.series[1] },
    ]
    : [{ name: "IV by delta", data: s.by_delta.map((x) => [x.delta, x.iv]), color: p.series[0] }], [s, mode, p]);
  const hist = useMemo<LineSeries[]>(() => [
    { name: "25Δ skew (put − call)", data: s.history.map((h) => [h.date, h.skew25_30]), color: p.series[0] },
    { name: "Put skew (put25 − ATM)", data: s.history.map((h) => [h.date, h.put_skew_30]), color: p.series[1] },
  ], [s.history, p]);
  return (
    <div className="stack">
      <Card><ExpiryPills expiries={s.expiries} value={s.expiry} onChange={setExpiry} /></Card>
      <Card title={`IV smile · ${s.expiry} (${s.dte} DTE)`} actions={
        <Segmented label="Smile axis" value={mode} onChange={setMode} options={[{ value: "strike", label: "By strike" }, { value: "delta", label: "By delta" }]} />
      } subtitle={mode === "delta" ? "x = signed delta: puts negative, calls positive" : undefined}>
        <Smile xFmt={mode === "strike" ? "num" : "num2"} series={smile} spot={mode === "strike" ? s.spot : null} />
      </Card>
      <Card title="30-day skew history" subtitle="vol points">
        {s.history.length ? <LineChart series={hist} yFmt="vol" height={240} /> : <InfoBox>Skew history starts when collection started.</InfoBox>}
      </Card>
    </div>
  );
}

export function SkewTab() {
  const sym = useSym();
  const [expiry, setExpiry] = useQueryValue("expiry");
  const st = useApi(() => api.skew(sym, expiry), [sym, expiry]);
  return <Async state={st}>{(s) => <SkewBody s={s} setExpiry={setExpiry} />}</Async>;
}

const IM_COLUMNS: ColumnDef[] = [
  { key: "expiry", label: "Expiry", fmt: "date" },
  { key: "dte", label: "DTE", fmt: "int" },
  { key: "straddle", label: "Straddle", fmt: "money" },
  { key: "straddle_pct", label: "Straddle %", fmt: "pct" },
  { key: "iv", label: "ATM IV", fmt: "vol" },
  { key: "iv_move", label: "±1σ move", fmt: "money" },
  { key: "iv_move_pct", label: "±1σ %", fmt: "pct" },
  { key: "low", label: "Range low", fmt: "money" },
  { key: "high", label: "Range high", fmt: "money" },
  { key: "earnings_in_window", label: "Earnings in window", fmt: "bool" },
];

export function ImpliedMovesTab() {
  const sym = useSym();
  const st = useApi(() => api.impliedMoves(sym), [sym]);
  const p = usePalette();
  return (
    <Async state={st}>{(m) => (
      <div className="stack">
        <Card title="Implied move by expiry" subtitle={<>Spot {fmt(m.spot, "money")} · <AsOf ts={m.asof} /></>}>
          <BarChart categories={m.rows.map((r) => shortDate(r.expiry))} yFmt="pct" height={240}
            series={[{ name: "Straddle %", data: m.rows.map((r) => r.straddle_pct), color: p.series[0] }, { name: "±1σ IV move %", data: m.rows.map((r) => r.iv_move_pct), color: p.series[2] }]} />
        </Card>
        <Card>
          <DataTable columns={IM_COLUMNS} rows={m.rows as unknown as ReportRow[]} rowLink={false} csvName={`${sym}-implied-moves`}
            rowClass={(r) => (r.earnings_in_window ? "row-flag" : undefined)} />
        </Card>
      </div>
    )}</Async>
  );
}

export function VolSeasonalityTab() {
  const sym = useSym();
  const st = useApi(() => api.volSeasonality(sym), [sym]);
  const p = usePalette();
  return (
    <Async state={st}>{(v) => (
      <div className="stack">
        <Card title="Average volatility by calendar month" subtitle="IV30 from stored history; HV20 from up to 10 years of prices">
          <BarChart categories={v.months.map((m) => MONTHS[m.month - 1])} yFmt="vol" height={280}
            series={[{ name: "Avg IV30", data: v.months.map((m) => m.avg_iv30), color: p.series[0] }, { name: "Avg HV20", data: v.months.map((m) => m.avg_hv20), color: p.series[1] }]} />
        </Card>
        <Card>
          <DataTable minimal rowLink={false} rows={v.months.map((m) => ({ ...m, month_name: MONTHS[m.month - 1] })) as unknown as ReportRow[]} columns={[
            { key: "month_name", label: "Month", fmt: "text" }, { key: "avg_iv30", label: "Avg IV30", fmt: "vol" },
            { key: "iv_samples", label: "IV samples (days)", fmt: "int" }, { key: "avg_hv20", label: "Avg HV20", fmt: "vol" }, { key: "hv_years", label: "HV years", fmt: "int" },
          ]} />
        </Card>
      </div>
    )}</Async>
  );
}
