// Small JSON file persistence: atomic replace (write temp + rename), missing/corrupt file → null.
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export interface JsonFile {
  read(): unknown;
  write(value: unknown): void;
}

export function jsonFile(path: string): JsonFile {
  return {
    read() {
      try { return JSON.parse(readFileSync(path, "utf8")) as unknown; } catch { return null; }
    },
    write(value) {
      mkdirSync(dirname(path), { recursive: true });
      const tmp = `${path}.${process.pid}.tmp`;
      writeFileSync(tmp, JSON.stringify(value, null, 2), { mode: 0o600 });
      renameSync(tmp, path);
    },
  };
}
