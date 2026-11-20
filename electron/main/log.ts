// Minimal logger: timestamped lines to stderr and (optionally) an append-only file.
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export interface Logger {
  info(msg: string): void;
  warn(msg: string): void;
  error(msg: string): void;
}

export function createLogger(file: string | null): Logger {
  if (file) { try { mkdirSync(dirname(file), { recursive: true }); } catch { file = null; } }
  const write = (level: string, msg: string) => {
    const line = `${new Date().toISOString()} ${level} ${msg}`;
    process.stderr.write(`${line}\n`);
    if (file) { try { appendFileSync(file, `${line}\n`); } catch { /* disk full / permissions: keep running */ } }
  };
  return { info: (m) => write("INFO", m), warn: (m) => write("WARN", m), error: (m) => write("ERROR", m) };
}

export const silentLogger: Logger = { info: () => undefined, warn: () => undefined, error: () => undefined };
