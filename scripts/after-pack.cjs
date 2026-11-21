// electron-builder afterPack hook: ad-hoc sign the macOS app (no Developer ID). Apple-silicon Macs refuse
// to run code with a broken or missing signature; after packing, Electron's original signature no longer
// matches. `codesign --deep` only signs code in the standard nested locations (Frameworks, Helpers, …),
// not the Mach-O files of the bundled Python in Contents/Resources/python, so those are signed one by one
// first, then the app itself, and the result is verified.
const { execFileSync } = require("node:child_process");
const { closeSync, lstatSync, openSync, readdirSync, readSync } = require("node:fs");
const { join } = require("node:path");

const MACHO = new Set([0xfeedface, 0xfeedfacf, 0xcefaedfe, 0xcffaedfe, 0xcafebabe, 0xbebafeca]);

function isMachO(path) {
  const fd = openSync(path, "r");
  try {
    const b = Buffer.alloc(4);
    return readSync(fd, b, 0, 4, 0) === 4 && MACHO.has(b.readUInt32BE(0));
  } finally { closeSync(fd); }
}

function machOFiles(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = lstatSync(p);
    if (st.isSymbolicLink()) continue;
    if (st.isDirectory()) machOFiles(p, out);
    else if (st.isFile() && st.size >= 4 && isMachO(p)) out.push(p);
  }
  return out;
}

exports.machOFiles = machOFiles;

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;
  const app = join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const nested = machOFiles(join(app, "Contents", "Resources", "python"));
  for (let i = 0; i < nested.length; i += 200) {
    execFileSync("codesign", ["--force", "--sign", "-", ...nested.slice(i, i + 200)], { stdio: "inherit" });
  }
  console.log(`  • ad-hoc signed ${nested.length} bundled Python binaries`);
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
  execFileSync("codesign", ["--verify", "--deep", "--strict", "--verbose=2", app], { stdio: "inherit" });
};
