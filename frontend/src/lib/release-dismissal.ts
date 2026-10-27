import { safeGetItem, safeSetItem } from "@/lib/storage-utils";
import type { ReleaseStatus } from "@/types/release";

/**
 * How long a release notice stays hidden once dismissed, per browser and per account.
 *
 *   info      until a newer release than the one dismissed is published
 *   warning   seven days, then it comes back while the version is still deprecated
 *   critical  never: an end-of-life version or a missing security fix is said on every page
 */
const KEY_PREFIX = "orcastra-release-dismissed";
const WARNING_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;

type Dismissal = { level: string; latest: string | null; at: number };

const key = (scopeKey: string) => `${KEY_PREFIX}:${scopeKey}`;

function read(scopeKey: string): Dismissal | null {
  try {
    const raw = safeGetItem(key(scopeKey));
    const value = raw ? (JSON.parse(raw) as Dismissal) : null;
    return value && typeof value.at === "number" ? value : null;
  } catch {
    return null;
  }
}

export function canDismiss(status: ReleaseStatus): boolean {
  return status.level === "info" || status.level === "warning";
}

export function isDismissed(status: ReleaseStatus, scopeKey: string, now = Date.now()): boolean {
  if (!canDismiss(status)) return false;
  const dismissal = read(scopeKey);
  if (!dismissal || dismissal.level !== status.level) return false;
  if (status.level === "info") return dismissal.latest === status.latest_version;
  return now - dismissal.at < WARNING_SNOOZE_MS;
}

export function dismiss(status: ReleaseStatus, scopeKey: string, now = Date.now()): void {
  if (!canDismiss(status)) return;
  safeSetItem(key(scopeKey), JSON.stringify({ level: status.level, latest: status.latest_version, at: now }));
}
