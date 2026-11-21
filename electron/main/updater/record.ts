// What the updater remembers across launches (update-state.json): the update that is being tried out
// (for rollback), versions never to install automatically again, the last version the user was told
// about, and a message for the user (e.g. after a rollback). Parsing is tolerant: a bad file → defaults.
export interface PendingUpdate {
  version: string;
  previousVersion: string;
  /** The previous version kept for a rollback: backups/OptionLens-<v>.app (macOS), -<v>.AppImage (Linux), -<v>-win-x64-setup.exe (Windows). */
  backupPath: string;
  /** Launches of `version` so far that have not been confirmed healthy. */
  launches: number;
  /** A rollback to previousVersion was started. */
  rollingBack: boolean;
}

export interface UpdateRecord {
  pending: PendingUpdate | null;
  skipped: string[];
  notifiedVersion: string | null;
  notice: string | null;
}

export const EMPTY_RECORD: UpdateRecord = { pending: null, skipped: [], notifiedVersion: null, notice: null };

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const strOrNull = (v: unknown): string | null => (typeof v === "string" ? v : null);

function parsePending(v: unknown): PendingUpdate | null {
  if (!isObj(v) || typeof v.version !== "string" || typeof v.previousVersion !== "string" || typeof v.backupPath !== "string") return null;
  return {
    version: v.version, previousVersion: v.previousVersion, backupPath: v.backupPath,
    launches: typeof v.launches === "number" && Number.isInteger(v.launches) && v.launches >= 0 ? v.launches : 0,
    rollingBack: v.rollingBack === true,
  };
}

export function parseUpdateRecord(raw: unknown): UpdateRecord {
  const o = isObj(raw) ? raw : {};
  return {
    pending: parsePending(o.pending),
    skipped: Array.isArray(o.skipped) ? o.skipped.filter((s): s is string => typeof s === "string").slice(-20) : [],
    notifiedVersion: strOrNull(o.notifiedVersion),
    notice: strOrNull(o.notice),
  };
}

export interface RecordStore { get(): UpdateRecord; set(r: UpdateRecord): void }
