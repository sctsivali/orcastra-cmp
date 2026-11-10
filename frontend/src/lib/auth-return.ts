import { safeCallbackPath } from "@/lib/auth-redirect";

/**
 * The page a sign-in was started for, kept for the length of the tab.
 *
 * next-auth sends a failed sign-in to `/auth/signin?error=...` or `/auth/error` without the
 * callbackUrl, so a retry would land on the home page. The sign-in page remembers the
 * requested page here and the retry recalls it. Every read is re-validated, and storage that
 * throws (private mode, blocked site data) only costs the return trip.
 */
const RETURN_PATH_KEY = "orcastra:auth-return";

export function rememberReturnPath(raw: string | null | undefined): void {
  const path = safeCallbackPath(raw);
  if (!path) return;
  try {
    window.sessionStorage.setItem(RETURN_PATH_KEY, path);
  } catch {
    // Nothing to do; the retry falls back to the home page.
  }
}

export function recallReturnPath(): string | null {
  try {
    return safeCallbackPath(window.sessionStorage.getItem(RETURN_PATH_KEY));
  } catch {
    return null;
  }
}

export function forgetReturnPath(): void {
  try {
    window.sessionStorage.removeItem(RETURN_PATH_KEY);
  } catch {
    // A stale entry is still re-validated on every read.
  }
}
