// The table-state rule shared with the engine's MCP server (engine/optionlens/mcp/shaping.py apply_table): both suites
// run the cases in engine/tests/fixtures/table-state-cases.json.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { ReportRow } from "../api/types";
import { applyTable, type TableState } from "./table";

const fixture = JSON.parse(readFileSync(new URL("../../engine/tests/fixtures/table-state-cases.json", import.meta.url), "utf8")) as {
  rows: ReportRow[]; cases: { name: string; state: Omit<TableState, "hidden">; expect: number[] }[];
};

describe("applyTable (shared cases with the MCP server)", () => {
  for (const c of fixture.cases) {
    it(c.name, () => {
      expect(applyTable(fixture.rows, { ...c.state, hidden: [] }).map((r) => r.id)).toEqual(c.expect);
    });
  }
});
