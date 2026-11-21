// Where the engine's Python and code live.
// Packaged app: <resources>/python, a relocatable CPython with the engine (`optionlens`) and its locked
// dependencies installed (scripts/bundle-engine.sh); where the interpreter sits in it depends on the OS
// (platform/python-layout.ts). Development: OPTIONLENS_PYTHON / OPTIONLENS_ENGINE_DIR, else <repo>/engine
// with its uv virtualenv (`cd engine && uv sync`).
import { existsSync } from "node:fs";
import { join } from "node:path";
import type { EngineCommand } from "./engine";

export interface PythonLayout {
  bundledPython(resourcesPath: string): string;
  venvPython(venvDir: string): string;
}

export interface PathInputs {
  packaged: boolean;
  resourcesPath: string;
  appPath: string;
  env: Record<string, string | undefined>;
  layout: PythonLayout;
}

export function resolveEngineCommand(p: PathInputs): EngineCommand {
  const base = { PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" };
  if (p.packaged) {
    return { python: p.layout.bundledPython(p.resourcesPath), env: base, cwd: p.resourcesPath };
  }
  const engineDir = p.env.OPTIONLENS_ENGINE_DIR ?? join(p.appPath, "engine");
  const venvPython = p.layout.venvPython(join(engineDir, ".venv"));
  const python = p.env.OPTIONLENS_PYTHON ?? (existsSync(venvPython) ? venvPython : "python3");
  return { python, env: { ...base, PYTHONPATH: engineDir }, cwd: engineDir };
}
