/**
 * One automatic reload after a chunk fails to download.
 *
 * A failed chunk is almost always the console being updated or restarted while a page was
 * open: the browser holds the old HTML and asks for a hash the server has dropped, or asks
 * while the server is down. One reload fetches the current build. A second would loop while
 * the server is down, so the mark below allows one per path per minute and the boundary then
 * offers a button.
 *
 * scripts/checks/check-no-console-reload.mjs forbids a document reload under the console and
 * terminal surfaces, since it tears down every live session. This helper is reached only from
 * the root and inventory error boundaries, after the failed page has unmounted.
 */

const MARK_PREFIX = "orcastra.chunk-reload.";
const MARK_TTL_MS = 60_000;

const CHUNK_MESSAGE =
  /Loading (?:CSS )?chunk [\w./-]+ failed|Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i;

export function isChunkLoadError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const { name, message } = error as { name?: unknown; message?: unknown };
  if (name === "ChunkLoadError") return true;
  return typeof message === "string" && CHUNK_MESSAGE.test(message);
}

function markKey(pathname: string): string {
  return `${MARK_PREFIX}${pathname || "/"}`;
}

function readMark(pathname: string): number | null {
  try {
    const raw = window.sessionStorage.getItem(markKey(pathname));
    const at = raw ? Number(raw) : NaN;
    return Number.isFinite(at) ? at : null;
  } catch {
    return null;
  }
}

/** Reload the document once for this path; false when a reload was already spent recently. */
export function reloadOnceForChunkError(pathname: string): boolean {
  if (typeof window === "undefined") return false;
  const at = readMark(pathname);
  if (at !== null && Date.now() - at < MARK_TTL_MS) return false;
  try {
    window.sessionStorage.setItem(markKey(pathname), String(Date.now()));
  } catch {
    // Without storage there is no way to bound the loop, so do not start one.
    return false;
  }
  window.location.reload();
  return true;
}

export function clearChunkReloadMark(pathname: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(markKey(pathname));
  } catch {
    // Nothing to clear.
  }
}
