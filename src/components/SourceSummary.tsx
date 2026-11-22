// The status fields every chain source shows in Settings → Data sources, identical for IBKR and Questrade
// (lib/sources.ts SourceView): state, data quality, last chain, last sweep, top failure reasons, usage.
import { failureText, type SourceView } from "../lib/sources";
import { StatGrid } from "./StatGrid";

export function SourceStateBadge({ v }: { v: SourceView }) {
  return <span className={`badge ${v.tone}`}>{v.state}</span>;
}

export function SourceSummary({ v }: { v: SourceView }) {
  return (
    <>
      <StatGrid items={[
        { label: "State", node: <SourceStateBadge v={v} /> },
        { label: "Data", value: v.dataQuality, fmt: "text", hint: "Known after the first chain is fetched." },
        { label: "Last chain", value: v.lastChainAt, fmt: "datetime", hint: `No chain from ${v.label} yet.` },
        { label: "Last sweep", value: v.lastSweepAt, fmt: "datetime", hint: "No sweep yet.", sub: v.lastSweepDetail ?? undefined },
        { label: "Failures (24 h)", node: v.failures.length
          ? <ul className="failure-list">{v.failures.map((f) => <li key={f.reason}>{failureText(f)}</li>)}</ul>
          : <span className="muted">none</span> },
        { label: "Usage", value: v.usage, fmt: "text", hint: "Not connected." },
      ]} />
      {v.problem && <div className="warn-box" role="alert">{v.problem}</div>}
    </>
  );
}
