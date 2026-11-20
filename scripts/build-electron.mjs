// Bundles the main process and the preload into dist-electron/ (CommonJS; npm dependencies stay
// external and are packaged by electron-builder).
import { build } from "esbuild";

const common = { bundle: true, platform: "node", format: "cjs", target: "node22", sourcemap: true, logLevel: "info", external: ["electron"] };

await build({ ...common, entryPoints: ["electron/main/index.ts"], outfile: "dist-electron/main.cjs", packages: "external" });
// Sandboxed preloads may only require("electron"): everything else is bundled in.
await build({ ...common, entryPoints: ["electron/preload/index.ts"], outfile: "dist-electron/preload.cjs" });
