// Renders the app icon (build/icon.png, 1024²) and the menu-bar template icons
// (resources/trayTemplate*.png, black + alpha as macOS expects) without any image dependencies.
// Run with `npm run icons`; the outputs are committed.
import { mkdirSync, writeFileSync } from "node:fs";
import { deflateSync } from "node:zlib";

const crcTable = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
const crc32 = (buf) => { let c = 0xffffffff; for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type), data]);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}
function png(size, pixel) {
  const raw = Buffer.alloc((size * 4 + 1) * size);
  const S = 4; // supersampling
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0;
    for (let x = 0; x < size; x++) {
      const acc = [0, 0, 0, 0];
      for (let sy = 0; sy < S; sy++) for (let sx = 0; sx < S; sx++) {
        const [r, g, b, a] = pixel((x + (sx + 0.5) / S) / size, (y + (sy + 0.5) / S) / size);
        acc[0] += r * a; acc[1] += g * a; acc[2] += b * a; acc[3] += a;
      }
      const o = y * (size * 4 + 1) + 1 + x * 4;
      const a = acc[3] / (S * S);
      raw[o] = a ? Math.round(acc[0] / acc[3]) : 0; raw[o + 1] = a ? Math.round(acc[1] / acc[3]) : 0; raw[o + 2] = a ? Math.round(acc[2] / acc[3]) : 0;
      raw[o + 3] = Math.round(a * 255);
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0); ihdr.writeUInt32BE(size, 4); ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

// Lens glyph in unit coordinates: ring centred at (0.43, 0.43), handle towards the bottom-right.
function lens(x, y, ring = 0.27, width = 0.09) {
  const d = Math.hypot(x - 0.43, y - 0.43);
  if (Math.abs(d - ring) < width / 2) return true;
  // handle: segment from (0.63,0.63) to (0.85,0.85)
  const t = Math.max(0, Math.min(1, ((x - 0.63) + (y - 0.63)) / (2 * 0.22)));
  const px = 0.63 + 0.22 * t, py = 0.63 + 0.22 * t;
  return Math.hypot(x - px, y - py) < width * 0.62;
}

function appIcon(x, y) {
  // macOS-style rounded square with a dark gradient and an accent lens
  const m = 0.1, r = 0.2;
  const qx = Math.max(m + r - x, 0, x - (1 - m - r)), qy = Math.max(m + r - y, 0, y - (1 - m - r));
  if (Math.hypot(qx, qy) > r) return [0, 0, 0, 0];
  const lx = (x - m) / (1 - 2 * m), ly = (y - m) / (1 - 2 * m);
  if (lens(lx, ly, 0.25, 0.08)) return [77, 163, 255, 1];
  // small chart line inside the lens
  const inside = Math.hypot(lx - 0.43, ly - 0.43) < 0.2;
  const chart = 0.5 - 0.12 * Math.sin((lx - 0.25) * 9);
  if (inside && Math.abs(ly - chart) < 0.022) return [94, 214, 160, 1];
  const shade = 30 + Math.round(18 * (1 - y));
  return [shade - 8, shade, shade + 14, 1];
}

const tray = (connected) => (x, y) => {
  if (lens(x, y, 0.3, 0.12)) return [0, 0, 0, 1];
  if (connected && Math.hypot(x - 0.43, y - 0.43) < 0.12) return [0, 0, 0, 1];
  return [0, 0, 0, 0];
};

mkdirSync("build", { recursive: true });
mkdirSync("resources", { recursive: true });
writeFileSync("build/icon.png", png(1024, appIcon));
for (const [name, connected] of [["trayTemplate", false], ["trayConnectedTemplate", true]]) {
  writeFileSync(`resources/${name}.png`, png(16, tray(connected)));
  writeFileSync(`resources/${name}@2x.png`, png(32, tray(connected)));
}
console.log("icons written");
