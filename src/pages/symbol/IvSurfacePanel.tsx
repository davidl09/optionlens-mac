// Skew page: the IV surface (expiry × delta / moneyness / strike) as a heatmap. A click on a reliable cell adds its
// contract to the calculator; cells whose contract is in the calculator are outlined.
import { useCallback, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { IvSurface, Leg, SurfaceMode, SurfaceRows } from "../../api/types";
import { useApi } from "../../api/useApi";
import { Heatmap, type HeatCell } from "../../components/charts/Charts";
import { HEAT_GRID } from "../../components/charts/common";
import { MoneyText } from "../../components/Money";
import { AsOf, Async, Card, InfoBox, Segmented, SourceBadge } from "../../components/ui";
import { cellText, isClickable, MODE_OPTIONS, ROW_OPTIONS, rowLabel, scaleKind, surfaceCells, tooltipHtml } from "../../lib/ivSurface";
import { contractKey, heldQty, useLegs } from "../../state/legs";
import { useViewDetails } from "../../state/view";
import { LegAddedToast, shortDate } from "./shared";

const rowHeight = (n: number) => (n <= 20 ? 24 : 18);

function SurfaceBody({ s, sym }: { s: IvSurface; sym: string }) {
  const { legs, addLeg } = useLegs();
  const held = useMemo(() => heldQty(legs), [legs]);
  const [lastAdded, setLastAdded] = useState<Leg | null>(null);
  const xLabels = useMemo(() => s.expiries.map((e) => shortDate(e.expiry)), [s.expiries]);
  const yLabels = useMemo(() => s.rows.map((v) => rowLabel(s.rows_by, v)), [s.rows, s.rows_by]);
  const cells = useMemo(() => surfaceCells(s, (c, expiry) => c.strike !== null && c.right !== null && held.has(contractKey(expiry, c.strike, c.right))), [s, held]);
  const earningsCol = s.expiries.findIndex((e) => e.earnings_expiry);
  const xMarks = useMemo(() => (earningsCol >= 0 ? [{ index: earningsCol, badge: "E" }] : []), [earningsCol]);
  const tooltip = useCallback((c: HeatCell) => tooltipHtml(s, c.x, c.y, xLabels[c.x]), [s, xLabels]);
  const text = useCallback((v: number) => cellText(s.mode, v), [s.mode]);
  const onCell = useCallback((hc: HeatCell) => {
    const c = s.cells[hc.y][hc.x];
    if (!isClickable(c)) return;
    // no price: the leg follows the live mid (and IV) in the calculator
    const leg: Leg = { kind: "option", right: c.right!, strike: c.strike!, expiry: s.expiries[hc.x].expiry, qty: 1, price: null, iv: null };
    addLeg(leg);
    setLastAdded(leg);
  }, [s, addLeg]);
  const t = s.thresholds;
  return (
    <>
      {s.mode === "chg_1d" && s.prior_date === null && <InfoBox>No earlier end-of-day chain is stored for {s.symbol} yet: the 1-day change appears once one is.</InfoBox>}
      {cells.length === 0
        ? <InfoBox>No implied volatilities in this chain.</InfoBox>
        : <Heatmap xLabels={xLabels} yLabels={yLabels} cells={cells} scale={scaleKind(s.mode)} valueFmt={s.mode === "iv" ? "vol" : "pct_signed"}
          cellText={text} tooltip={tooltip} onCellClick={onCell} xMarks={xMarks} ariaLabel="IV surface heatmap"
          height={HEAT_GRID.top + HEAT_GRID.bottom + s.rows.length * rowHeight(s.rows.length)}
          legend={<>
            <span className="legend-inline"><span className="swatch hatch" /> unreliable: no bid, spread &gt; {Math.round(t.max_spread_of_mid * 100)}% of mid, OI &lt; {t.min_oi}, no IV, or not bracketed</span>
            <span className="legend-inline"><span className="swatch outlined" /> in the calculator</span>
            {earningsCol >= 0 && s.earnings && (
              <span className="legend-inline" data-testid="earnings-marker" title={`Earnings ${s.earnings.date}${s.earnings.reaction_date ? ` (reaction ${s.earnings.reaction_date})` : ""}: ${s.expiries[earningsCol].expiry} is the first expiry after it`}>
                <span className="heat-badge">E</span> first expiry after earnings {shortDate(s.earnings.date)}
              </span>
            )}
          </>} />}
      {lastAdded && <LegAddedToast sym={sym} leg={lastAdded} count={legs.length} onDismiss={() => setLastAdded(null)} />}
    </>
  );
}

export function IvSurfacePanel({ sym }: { sym: string }) {
  const [rows, setRows] = useState<SurfaceRows>("delta");
  const [mode, setMode] = useState<SurfaceMode>("iv");
  const st = useApi(() => api.ivSurface(sym, rows, mode), [sym, rows, mode]);
  useViewDetails({ surface_rows: rows, surface_colour: mode });
  const s = st.data;
  return (
    <Card title="IV surface" subtitle={s ? <>
      Spot <MoneyText value={s.spot} /> · <AsOf ts={s.asof} />{s.prior_date ? ` · change vs ${s.prior_date} close` : ""}
      {(s.source || s.delayed) && <> · <SourceBadge source={s.source} delayed={s.delayed} /></>}
    </> : undefined} actions={
      <div className="toolbar">
        <Segmented label="Rows" value={rows} onChange={setRows} options={ROW_OPTIONS} />
        <Segmented label="Colour" value={mode} onChange={setMode} options={MODE_OPTIONS} />
      </div>
    }>
      <Async state={st}>{(d) => <SurfaceBody s={d} sym={sym} />}</Async>
    </Card>
  );
}
