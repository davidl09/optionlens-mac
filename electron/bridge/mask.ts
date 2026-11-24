// Account numbers are never logged or shown in full (INTEGRATION.md §11): only their last 3 characters.
export function maskAccount(account: string): string {
  const a = String(account).trim();
  return `•••${a.length > 3 ? a.slice(-3) : ""}`;
}
