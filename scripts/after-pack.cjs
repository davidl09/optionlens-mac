// electron-builder afterPack hook: ad-hoc sign the macOS app (no Developer ID). Apple-silicon Macs refuse
// to run code with a broken signature; after packing, Electron's original signature no longer matches.
const { execFileSync } = require("node:child_process");
const { join } = require("node:path");

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;
  const app = join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
};
