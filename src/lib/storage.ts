// localStorage access that never throws (private windows, blocked storage).
export function loadPref(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}

export function savePref(key: string, value: string): void {
  try { window.localStorage.setItem(key, value); } catch { /* ignore */ }
}
