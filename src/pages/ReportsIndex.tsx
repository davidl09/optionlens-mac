// /reports — every report definition, grouped by category. ?category= filters, ?symbols= is passed on.
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, qs } from "../api/client";
import type { ReportDef } from "../api/types";
import { useApi } from "../api/useApi";
import { Async, Card, InfoBox, Segmented } from "../components/ui";

const CATEGORIES: ReportDef["category"][] = ["Options", "Strategies", "Earnings", "Volatility", "Stocks", "Dividends"];

export default function ReportsIndex() {
  const [sp, setSp] = useSearchParams();
  const defs = useApi(api.reportDefs, []);
  const [q, setQ] = useState("");
  const category = sp.get("category") ?? "";
  const symbols = sp.get("symbols") ?? "";
  const setCategory = (c: string) => { const n = new URLSearchParams(sp); if (c) n.set("category", c); else n.delete("category"); setSp(n, { replace: true }); };
  const suffix = qs({ symbols });

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1 className="page-title">Reports & screeners</h1>
          <p className="page-desc">Universe-wide screeners. Every report supports column filters, saved presets and CSV export.</p>
        </div>
      </div>
      {symbols && <InfoBox title="Symbol scope:">Links below open with <b>symbols={symbols}</b> pre-filled. <Link to="/reports">Clear</Link></InfoBox>}
      <div className="toolbar">
        <Segmented label="Category" value={category} onChange={setCategory} size="md"
          options={[{ value: "", label: "All" }, ...CATEGORIES.map((c) => ({ value: c, label: c }))]} />
        <input type="search" placeholder="Filter reports…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter reports" />
      </div>
      <Async state={defs}>{(list) => <Groups list={list} category={category} q={q} suffix={suffix} />}</Async>
    </div>
  );
}

function Groups({ list, category, q, suffix }: { list: ReportDef[]; category: string; q: string; suffix: string }) {
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return CATEGORIES.filter((c) => !category || c === category).map((c) => ({
      c, defs: list.filter((d) => d.category === c && (!needle || `${d.title} ${d.description} ${d.id}`.toLowerCase().includes(needle))),
    })).filter((g) => g.defs.length);
  }, [list, category, q]);
  if (!groups.length) return <InfoBox>No reports match.</InfoBox>;
  return (
    <>
      {groups.map((g) => (
        <section key={g.c} className="report-group">
          <h2 className="section-title">{g.c} <small className="muted">{g.defs.length}</small></h2>
          <div className="report-cards">
            {g.defs.map((d) => (
              <Link key={d.id} to={`/reports/${d.id}${suffix}`} className="report-card">
                <Card title={d.title}><p>{d.description}</p><span className="muted small mono">{d.id}</span></Card>
              </Link>
            ))}
          </div>
        </section>
      ))}
    </>
  );
}
