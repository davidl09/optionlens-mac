// Test fixtures: minimal PE files with a version resource (as makensis writes it).
const pad4 = (b: Buffer) => Buffer.concat([b, Buffer.alloc((4 - (b.length % 4)) % 4)]);

/** A VERSIONINFO block as rc.exe / makensis write it. */
function block(key: string, value: Buffer, text: boolean, children: Buffer[] = []): Buffer {
  const head = pad4(Buffer.concat([Buffer.alloc(6), Buffer.from(`${key}\0`, "utf16le")]));
  const body = Buffer.concat([head, children.length ? pad4(value) : value, ...children.map((c, i) => (i < children.length - 1 ? pad4(c) : c))]);
  body.writeUInt16LE(body.length, 0);
  body.writeUInt16LE(text ? value.length / 2 : value.length, 2);
  body.writeUInt16LE(text ? 1 : 0, 4);
  return body;
}
const str = (k: string, v: string) => block(k, Buffer.from(`${v}\0`, "utf16le"), true);

export function versionResource(strings: Record<string, string>, product: [number, number, number, number]): Buffer {
  const fixed = Buffer.alloc(52);
  fixed.writeUInt32LE(0xfeef04bd, 0);
  fixed.writeUInt32LE(0x10000, 4);
  for (const o of [8, 16]) { fixed.writeUInt32LE(((product[0] << 16) | product[1]) >>> 0, o); fixed.writeUInt32LE(((product[2] << 16) | product[3]) >>> 0, o + 4); }
  const table = block("040904b0", Buffer.alloc(0), true, Object.entries(strings).map(([k, v]) => str(k, v)));
  return block("VS_VERSION_INFO", fixed, false, [block("StringFileInfo", Buffer.alloc(0), true, [table]), block("VarFileInfo", Buffer.alloc(0), true)]);
}

/** A minimal PE file: DOS header, PE header, one section holding `section`, then `overlay` (like NSIS data). */
export function peFile(section: Buffer, overlay = Buffer.alloc(0)): Buffer {
  const header = Buffer.alloc(0x200);
  header.write("MZ", 0, "latin1");
  header.writeUInt32LE(0x40, 0x3c);
  header.write("PE\0\0", 0x40, "latin1");
  header.writeUInt16LE(1, 0x46);            // NumberOfSections
  header.writeUInt16LE(0, 0x54);            // SizeOfOptionalHeader
  const s = 0x40 + 24;                      // section table
  header.write(".rsrc", s, "latin1");
  header.writeUInt32LE(section.length, s + 16);
  header.writeUInt32LE(0x200, s + 20);
  return Buffer.concat([header, section, overlay]);
}
