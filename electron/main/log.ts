// Minimal logger: timestamped lines to stderr and (optionally) a file, rotated by size
// (main.log → main.log.1 → … → main.log.<keep>; the oldest is dropped).
import { appendFileSync, mkdirSync, renameSync, rmSync, statSync } from "node:fs";
import { dirname } from "node:path";

export interface Logger {
  info(msg: string): void;
  warn(msg: string): void;
  error(msg: string): void;
}

export interface LogRotation { maxBytes: number; keep: number }
export const DEFAULT_ROTATION: LogRotation = { maxBytes: 10 * 1024 * 1024, keep: 3 };

function rotate(file: string, keep: number): void {
  rmSync(`${file}.${keep}`, { force: true });
  for (let i = keep - 1; i >= 1; i--) {
    try { renameSync(`${file}.${i}`, `${file}.${i + 1}`); } catch { /* not there yet */ }
  }
  try { renameSync(file, `${file}.1`); } catch { /* not there yet */ }
}

export function createLogger(file: string | null, rotation: LogRotation = DEFAULT_ROTATION): Logger {
  if (file) { try { mkdirSync(dirname(file), { recursive: true }); } catch { file = null; } }
  let size = 0;
  if (file) { try { size = statSync(file).size; } catch { size = 0; } }
  const write = (level: string, msg: string) => {
    const line = `${new Date().toISOString()} ${level} ${msg}\n`;
    process.stderr.write(line);
    if (!file) return;
    try {
      const bytes = Buffer.byteLength(line);
      if (size > 0 && size + bytes > rotation.maxBytes) { rotate(file, rotation.keep); size = 0; }
      appendFileSync(file, line);
      size += bytes;
    } catch { /* disk full / permissions: keep running */ }
  };
  return { info: (m) => write("INFO", m), warn: (m) => write("WARN", m), error: (m) => write("ERROR", m) };
}

export const silentLogger: Logger = { info: () => undefined, warn: () => undefined, error: () => undefined };
