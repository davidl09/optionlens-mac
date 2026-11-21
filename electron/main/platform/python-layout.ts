// Where python-build-standalone (the bundled engine, scripts/bundle-engine.sh) and a virtualenv keep the
// interpreter: bin/ on macOS and Linux, the folder itself (bundle) or Scripts\ (virtualenv) on Windows.
import { join } from "node:path";

export const posixPython = {
  bundledPython: (resources: string) => join(resources, "python", "bin", "python3"),
  venvPython: (venv: string) => join(venv, "bin", "python"),
};

export const windowsPython = {
  bundledPython: (resources: string) => join(resources, "python", "python.exe"),
  venvPython: (venv: string) => join(venv, "Scripts", "python.exe"),
};
