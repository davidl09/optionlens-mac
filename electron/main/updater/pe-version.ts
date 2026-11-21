// The version resource of a Windows program (PE file): what Explorer shows under Properties → Details. The
// Windows updater checks a downloaded setup.exe with it, as the macOS updater checks an app's Info.plist:
// ProductName must be OptionLens and ProductVersion the release's version. The resource is read from the
// PE image itself (the sections); the NSIS payload appended after them is never searched.
export interface PeVersion {
  /** VS_FIXEDFILEINFO product version, "a.b.c.d". */
  fixedProductVersion: string;
  /** StringFileInfo entries (ProductName, ProductVersion, FileVersion, CompanyName, …). */
  strings: Record<string, string>;
}

const u16 = (b: Buffer, o: number) => b.readUInt16LE(o);
const u32 = (b: Buffer, o: number) => b.readUInt32LE(o);
const align4 = (n: number) => (n + 3) & ~3;

/** End of the PE image in the file (the last section's raw data), or null when `buf` is not a PE file. */
export function peImageEnd(buf: Buffer): number | null {
  if (buf.length < 0x40 || buf.toString("latin1", 0, 2) !== "MZ") return null;
  const pe = u32(buf, 0x3c);
  if (pe + 24 > buf.length || buf.toString("latin1", pe, pe + 4) !== "PE\0\0") return null;
  const sections = u16(buf, pe + 6);
  const table = pe + 24 + u16(buf, pe + 20);
  let end = table + 40 * sections;
  for (let i = 0; i < sections; i++) {
    const s = table + 40 * i;
    if (s + 40 > buf.length) return null;
    end = Math.max(end, u32(buf, s + 20) + u32(buf, s + 16));   // PointerToRawData + SizeOfRawData
  }
  return end;
}

interface Block { key: string; value: Buffer; text: boolean; children: Block[] }

function utf16z(buf: Buffer, start: number, limit: number): string {
  let end = start;
  while (end + 1 < limit && u16(buf, end) !== 0) end += 2;
  return buf.toString("utf16le", start, end);
}

/** One VERSIONINFO block (wLength, wValueLength, wType, szKey, padding, value, padding, children). */
function parseBlock(buf: Buffer, off: number, limit: number): Block {
  const length = u16(buf, off);
  const end = off + length;
  if (length < 6 || end > limit) throw new Error("bad version block");
  const valueLength = u16(buf, off + 2);
  const text = u16(buf, off + 4) === 1;
  const key = utf16z(buf, off + 6, end);
  const valueStart = align4(off + 6 + (key.length + 1) * 2);
  const valueBytes = text ? valueLength * 2 : valueLength;
  const value = buf.subarray(valueStart, Math.min(valueStart + valueBytes, end));
  const children: Block[] = [];
  for (let p = align4(valueStart + valueBytes); p + 6 <= end;) {
    const child = parseBlock(buf, p, end);
    children.push(child);
    p = align4(p + Math.max(6, u16(buf, p)));
  }
  return { key, value, text, children };
}

/** The version resource of the PE file in `buf` (its first part is enough), or null when it has none. */
export function readPeVersion(buf: Buffer): PeVersion | null {
  const imageEnd = peImageEnd(buf);
  if (imageEnd === null) return null;
  const image = buf.subarray(0, Math.min(imageEnd, buf.length));
  const at = image.indexOf(Buffer.from("VS_VERSION_INFO\0", "utf16le"));
  if (at < 6) return null;
  let root: Block;
  try { root = parseBlock(image, at - 6, image.length); } catch { return null; }
  if (root.value.length < 52 || u32(root.value, 0) !== 0xfeef04bd) return null;
  const ms = u32(root.value, 16), ls = u32(root.value, 20);
  const strings: Record<string, string> = {};
  for (const table of root.children.filter((c) => c.key === "StringFileInfo").flatMap((c) => c.children)) {
    for (const s of table.children) strings[s.key] = s.value.toString("utf16le").replace(/\0.*$/s, "");
  }
  return { fixedProductVersion: `${ms >>> 16}.${ms & 0xffff}.${ls >>> 16}.${ls & 0xffff}`, strings };
}
