// Where the engine's Python and code live.
// Packaged app: Resources/python (relocatable CPython with the engine's locked dependencies installed)
// and Resources/engine (the `optionlens` package). Development: OPTIONLENS_PYTHON / OPTIONLENS_ENGINE_DIR,
// else <repo>/engine with its uv virtualenv.
import { existsSync } from "node:fs";
import { join } from "node:path";
import type { EngineCommand } from "./engine";

export interface PathInputs {
  packaged: boolean;
  resourcesPath: string;
  appPath: string;
  env: Record<string, string | undefined>;
}

export function resolveEngineCommand(p: PathInputs): EngineCommand {
  const base = { PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" };
  if (p.packaged) {
    const engineDir = join(p.resourcesPath, "engine");
    return { python: join(p.resourcesPath, "python", "bin", "python3"), env: { ...base, PYTHONPATH: engineDir }, cwd: engineDir };
  }
  const engineDir = p.env.OPTIONLENS_ENGINE_DIR ?? join(p.appPath, "engine");
  const venvPython = join(engineDir, ".venv", "bin", "python");
  const python = p.env.OPTIONLENS_PYTHON ?? (existsSync(venvPython) ? venvPython : "python3");
  return { python, env: { ...base, PYTHONPATH: engineDir }, cwd: engineDir };
}
