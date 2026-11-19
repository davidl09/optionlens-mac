// Options group: activity (volume) and open interest.
import { useMemo } from "react";
import { api } from "../../api/client";
import type { Activity, ColumnDef, OpenInterest, ReportRow } from "../../api/types";
import { useApi } from "../../api/useApi";
import { BarChart, LineChart, RefLine } from "../../components/charts/Charts";
import { usePalette } from "../../components/charts/EChart";
import { DataTable } from "../../components/DataTable";
import { StatGrid } from "../../components/StatGrid";
import { AsOf, Async, Card, InfoBox } from "../../components/ui";
import { fmt } from "../../lib/format";
import { ExpiryPills, shortDate, useQueryValue, useSym } from "./shared";

const CONTRACT_COLUMNS: ColumnDef[] = [
  { key: "osi", label: "Contract", fmt: "osi" },
  { key: "expiry", label: "Expiry", fmt: "date" },
  { key: "right", label: "Type", fmt: "badge" },
  { key: "strike", label: "Strike", fmt: "num" },
  { key: "volume", label: "Volume", fmt: "int" },
  { key: "oi", label: "OI", fmt: "int" },
  { key: "vol_oi", label: "Vol/OI", fmt: "ratio" },
  { key: "premium", label: "Premium", fmt: "bigmoney", description: "Volume × mid × 100" },
  { key: "mid", label: "Mid", fmt: "money" },
  { key: "iv", label: "IV", fmt: "vol" },
  { key: "delta", label: "Δ", fmt: "num2" },
];

const OI_CHANGE_COLUMNS: ColumnDef[] = [
  { key: "osi", label: "Contract", fmt: "osi" },
  { key: "right", label: "Type", fmt: "badge" },
  { key: "strike", label: "Strike", fmt: "num" },
  { key: "oi", label: "OI", fmt: "int" },
  { key: "oi_prev", label: "Prior OI", fmt: "int" },
  { key: "oi_change", label: "Change", fmt: "int" },
  { key: "volume", label: "Volume", fmt: "int" },
  { key: "iv", label: "IV", fmt: "vol" },
];

/** Contract rows carry no symbol field; add it so OSI cells link to the contract page. */
function withSymbol<T extends object>(rows: T[], symbol: string): ReportRow[] {
  return rows.map((r) => ({ ...r, symbol })) as ReportRow[];
}

function ActivityBody({ a, sym, setExpiry }: { a: Activity; sym: string; setExpiry: (e: string) => void }) {
  const p = usePalette();
  const callC = p.series[0], putC = p.series[1];
  const exp = useMemo(() => ({ cats: a.by_expiry.map((e) => shortDate(e.expiry)), ...a }), [a]);
  const hist = useMemo(() => [
    { name: "Call volume", data: a.history.map((h) => [h.date, h.call_volume] as [string, number | null]), color: callC },
    { name: "Put volume", data: a.history.map((h) => [h.date, h.put_volume] as [string, number | null]), color: putC },
  ], [a.history, callC, putC]);
  const t = a.totals, m = a.moneyness;
  return (
    <div className="stack">
      <Card title="Totals" subtitle={<AsOf ts={a.asof} />}>
        <StatGrid items={[
          { label: "Call volume", value: t.call_volume, fmt: "int" }, { label: "Put volume", value: t.put_volume, fmt: "int" },
          { label: "P/C volume", value: t.pc_volume, fmt: "ratio" }, { label: "Call OI", value: t.call_oi, fmt: "int" },
          { label: "Put OI", value: t.put_oi, fmt: "int" }, { label: "P/C OI", value: t.pc_oi, fmt: "ratio" },
          { label: "Call premium", value: t.call_premium, fmt: "bigmoney" }, { label: "Put premium", value: t.put_premium, fmt: "bigmoney" },
        ]} />
      </Card>
      <div className="grid-2">
        <Card title="Volume by expiry" subtitle="click a bar to drill into that expiry">
          <BarChart categories={exp.cats} yFmt="int" onClickCategory={(c) => { const e = a.by_expiry[exp.cats.indexOf(c)]; if (e) setExpiry(e.expiry); }}
            series={[{ name: "Calls", data: a.by_expiry.map((e) => e.call_volume), color: callC }, { name: "Puts", data: a.by_expiry.map((e) => e.put_volume), color: putC }]} />
        </Card>
        <Card title="Open interest by expiry">
          <BarChart categories={exp.cats} yFmt="int"
            series={[{ name: "Call OI", data: a.by_expiry.map((e) => e.call_oi), color: callC }, { name: "Put OI", data: a.by_expiry.map((e) => e.put_oi), color: putC }]} />
        </Card>
      </div>
      <div className="grid-main-side">
        <Card title={`Volume by strike${a.expiry ? ` · ${a.expiry}` : " · all expiries"}`}>
          <BarChart categories={a.by_strike.map((s) => fmt(s.strike, "num"))} yFmt="int" height={300}
            series={[{ name: "Calls", data: a.by_strike.map((s) => s.call_volume), color: callC }, { name: "Puts", data: a.by_strike.map((s) => s.put_volume), color: putC }]} />
        </Card>
        <Card title="Volume by moneyness" subtitle="ATM = within ±2.5% of spot">
          <BarChart categories={["OTM", "ATM", "ITM"]} yFmt="int" height={300}
            series={[{ name: "Calls", data: [m.call_otm, m.call_atm, m.call_itm], color: callC }, { name: "Puts", data: [m.put_otm, m.put_atm, m.put_itm], color: putC }]} />
        </Card>
      </div>
      <Card title="Call / put volume history" subtitle="from daily collected metrics">
        {a.history.length ? <LineChart series={hist} yFmt="int" height={220} /> : <InfoBox>History starts on the first day the collector ran.</InfoBox>}
      </Card>
      <Card title="Top 20 contracts by volume">
        <DataTable columns={CONTRACT_COLUMNS} rows={withSymbol(a.top_contracts, sym)} csvName={`${sym}-top-contracts`} rowLink={(r) => `/s/${sym}/contract/${encodeURIComponent(String(r.osi))}`} />
      </Card>
    </div>
  );
}

export function ActivityTab() {
  const sym = useSym();
  const [expiry, setExpiry] = useQueryValue("expiry", "all");
  const st = useApi(() => api.activity(sym, expiry), [sym, expiry]);
  return (
    <Async state={st}>{(a) => (
      <div className="stack">
        <Card><ExpiryPills expiries={a.by_expiry} value={a.expiry ?? "all"} onChange={setExpiry} allowAll /></Card>
        <ActivityBody a={a} sym={sym} setExpiry={setExpiry} />
      </div>
    )}</Async>
  );
}

function OIBody({ o, sym }: { o: OpenInterest; sym: string }) {
  const p = usePalette();
  const callC = p.series[0], putC = p.series[1];
  const mp = o.max_pain.find((x) => x.expiry === o.expiry)?.strike ?? null;
  const cats = o.by_strike.map((s) => fmt(s.strike, "num"));
  const refs = useMemo<RefLine[] | undefined>(() => (mp !== null ? [{ x: fmt(mp, "num"), label: `max pain ${fmt(mp, "num")}`, color: p.series[3] }] : undefined), [mp, p]);
  const changeHint = "OI change needs the prior trading day's stored end-of-day chain.";
  return (
    <div className="stack">
      <Card title={`Open interest by strike${o.expiry ? ` · ${o.expiry}` : ""}`}>
        <BarChart categories={cats} yFmt="int" height={300} refLines={refs}
          series={[{ name: "Call OI", data: o.by_strike.map((s) => s.call_oi), color: callC }, { name: "Put OI", data: o.by_strike.map((s) => s.put_oi), color: putC }]} />
      </Card>
      <Card title="OI change vs prior day" subtitle={o.prior_date ? `vs ${o.prior_date}` : undefined}>
        {o.prior_date
          ? <BarChart categories={cats} yFmt="int" height={240}
              series={[{ name: "Call Δ OI", data: o.by_strike.map((s) => s.call_oi_change), color: callC }, { name: "Put Δ OI", data: o.by_strike.map((s) => s.put_oi_change), color: putC }]} />
          : <InfoBox>{changeHint}</InfoBox>}
      </Card>
      <div className="grid-3">
        <Card title="Max pain by expiry">
          <DataTable minimal rowLink={false} rows={o.max_pain as unknown as ReportRow[]}
            columns={[{ key: "expiry", label: "Expiry", fmt: "date" }, { key: "strike", label: "Max pain strike", fmt: "num" }]} maxHeight={360} />
        </Card>
        <Card title="Largest OI increases">
          {o.prior_date ? <DataTable minimal columns={OI_CHANGE_COLUMNS} rows={withSymbol(o.top_increases, sym)} rowLink={false} maxHeight={360} /> : <InfoBox>{changeHint}</InfoBox>}
        </Card>
        <Card title="Largest OI decreases">
          {o.prior_date ? <DataTable minimal columns={OI_CHANGE_COLUMNS} rows={withSymbol(o.top_decreases, sym)} rowLink={false} maxHeight={360} /> : <InfoBox>{changeHint}</InfoBox>}
        </Card>
      </div>
    </div>
  );
}

export function OpenInterestTab() {
  const sym = useSym();
  const [expiry, setExpiry] = useQueryValue("expiry");
  const st = useApi(() => api.openInterest(sym, expiry), [sym, expiry]);
  return (
    <Async state={st}>{(o) => (
      <div className="stack">
        <Card><ExpiryPills expiries={o.expiries} value={o.expiry} onChange={setExpiry} /></Card>
        <OIBody o={o} sym={sym} />
      </div>
    )}</Async>
  );
}
