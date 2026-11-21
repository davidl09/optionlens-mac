// Reading freedesktop.org desktop entries (.desktop files): the keys of the [Desktop Entry] group.
// Used for the Linux autostart entry (platform/linux.ts) and the entry inside an AppImage (updater).
export function desktopEntryKeys(content: string): Map<string, string> {
  const keys = new Map<string, string>();
  let group = "";
  for (const raw of content.split(/\r?\n/)) {
    const line = raw.trim();
    if (line.startsWith("[")) { group = line; continue; }
    const eq = line.indexOf("=");
    if (group === "[Desktop Entry]" && eq > 0 && !line.startsWith("#")) keys.set(line.slice(0, eq).trim(), line.slice(eq + 1).trim());
  }
  return keys;
}
