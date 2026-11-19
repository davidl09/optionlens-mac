// /compare/volatility and /compare/straddle
import { FormEvent, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { CompareVolatility } from "../api/types";
import { useApi } from "../api/useApi";
import { LineChart, LineSeries } from "../components/charts/Charts";
import { usePalette } from "../components/charts/EChart";
import { DataTable, inferColumns } from "../components/DataTable";
import { Async, Card, InfoBox } from "../components/ui";
import { parseSymbols } from "../lib/params";

const MAX_SERIES = 8;  // categorical palette size; more symbols would recycle colours

function SymbolsForm({ initial, extra, onSubmit }: { initial: string; extra?: React.ReactNode; onSubmit: (symbols: string) => void }) {
  const [v, setV] = useState(initial);
  const submit = (e: FormEvent) => { e.preventDefault(); onSubmit(parseSymbols(v).join(",")); };
  return (
    <form className="param-form" onSubmit={submit}>
      <div className="param"><label htmlFor="cmp-symbols">Symbols</label><input id="cmp-symbols" className="w-wide" value={v} onChange={(e) => setV(e.target.value.toUpperCase())} placeholder="SPY, QQQ, IWM" /></div>
      {extra}
      <div className="param param-submit"><button className="btn btn-primary" type="submit">Compare</button></div>
    </form>
  );
}

function VolBody({ c }: { c: CompareVolatility }) {
  const p = usePalette();
  const [showHv, setShowHv] = useState(false);
  const series = useMemo<LineSeries[]>(() => c.series.slice(0, MAX_SERIES).flatMap((s, i) => {
    const color = p.series[i];
    const out: LineSeries[] = [{ name: `${s.symbol} IV30`, data: s.points.map((x) => [x.date, x.iv30]), color }];
    if (showHv) out.push({ name: `${s.symbol} HV20`, data: s.points.map((x) => [x.date, x.hv20]), color, dashed: true });
    return out;
  }), [c.series, showHv, p]);
  return (
    <div className="stack">
      <Card title="IV30 overlay" actions={<label className="check-inline"><input type="checkbox" checked={showHv} onChange={(e) => setShowHv(e.target.checked)} /> HV20 (dashed)</label>}>
        <LineChart series={series} yFmt="vol" height={340} zoom />
      </Card>
      <Card title="Comparison">
        <DataTable columns={inferColumns(c.table)} rows={c.table} csvName="compare-volatility" minimal />
      </Card>
    </div>
  );
}

export function CompareVolatilityPage() {
  const [sp, setSp] = useSearchParams();
  const symbols = sp.get("symbols") ?? "SPY,QQQ,IWM";
  const list = parseSymbols(symbols);
  const st = useApi(list.length ? () => api.compareVolatility(list) : null, [symbols]);
  return (
    <div className="stack">
      <div className="page-head"><div><h1 className="page-title">Compare implied & historical volatility</h1></div></div>
      <Card><SymbolsForm initial={symbols} onSubmit={(s) => setSp({ symbols: s })} /></Card>
      {list.length > MAX_SERIES && <InfoBox>Only the first {MAX_SERIES} symbols are charted; all appear in the table.</InfoBox>}
      <Async state={st}>{(c) => <VolBody c={c} />}</Async>
    </div>
  );
}

export function CompareStraddlePage() {
  const [sp, setSp] = useSearchParams();
  const symbols = sp.get("symbols") ?? "SPY,QQQ,IWM";
  const dte = Number(sp.get("dte") ?? 30);
  const [dteInput, setDteInput] = useState(String(dte));
  const list = parseSymbols(symbols);
  const st = useApi(list.length ? () => api.compareStraddle(list, dte) : null, [symbols, dte]);
  return (
    <div className="stack">
      <div className="page-head"><div><h1 className="page-title">Compare straddles & implied moves</h1><p className="page-desc">ATM straddle of the expiry nearest the target DTE for each symbol.</p></div></div>
      <Card>
        <SymbolsForm initial={symbols} onSubmit={(s) => setSp({ symbols: s, dte: dteInput || "30" })}
          extra={<div className="param"><label htmlFor="cmp-dte">Target DTE</label><input id="cmp-dte" type="number" className="w-num" value={dteInput} onChange={(e) => setDteInput(e.target.value)} /></div>} />
      </Card>
      <Async state={st}>{(r) => (
        <>
          {r.notes.length > 0 && <InfoBox>{r.notes.join(" ")}</InfoBox>}
          <Card className="flush"><DataTable columns={r.columns} rows={r.rows} csvName="compare-straddle" /></Card>
        </>
      )}</Async>
    </div>
  );
}
