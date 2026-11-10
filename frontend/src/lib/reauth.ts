/**
 * Signing in again from the page that needs it, and back to the same page.
 *
 * important: a tab opened from a deep link (ex; an OrcaHub terminal or console link) whose session
 * the IdP has ended has nothing to keep, so it signs in again on its own; that is silent while the
 * IdP session lives. A tab that was working keeps its page and offers a button instead, since a
 * navigation ends its open sessions. An automatic attempt that comes back here within a minute
 * did not stick, and a second would only bounce between the two hosts, so the button shows.
 */
import { signIn } from "next-auth/react";
import { safeCallbackPath } from "@/lib/auth-redirect";

const LAST_AUTO_KEY = "orcastra:reauth:last";
const AUTO_LOOP_MS = 60_000;

let leaving = false;
let worked = false;

/** This tab has had a working session (auth-provider.tsx); a sign-in now waits for the operator. */
export function noteSessionWorked(): void {
  worked = true;
}

/** True once this page started a sign-in; its leave prompts stand aside for it. */
export function isReauthenticating(): boolean {
  return leaving;
}

function lastAutoAttempt(): number {
  try {
    return Number(window.sessionStorage.getItem(LAST_AUTO_KEY) || 0);
  } catch {
    return 0;
  }
}

function markAutoAttempt(): void {
  try {
    window.sessionStorage.setItem(LAST_AUTO_KEY, String(Date.now()));
  } catch {
    // Storage blocked: the sign-in still happens, only the loop guard is lost.
  }
}

/** True when an automatic sign-in may start now (none in the last minute for this tab). */
function mayReauthenticateAutomatically(): boolean {
  return Date.now() - lastAutoAttempt() >= AUTO_LOOP_MS;
}

/**
 * Sign in again and come back to this page. `auto` marks an attempt the operator did not ask
 * for; it is held back (returns false) in a tab that has worked, or when another one started in
 * the last minute. Returns true when it navigates.
 */
export function reauthenticate({ auto }: { auto: boolean }): boolean {
  if (auto) {
    if (worked || !mayReauthenticateAutomatically()) return false;
    markAutoAttempt();
  }
  leaving = true;
  const here = safeCallbackPath(`${window.location.pathname}${window.location.search}`) ?? "/";
  signIn("authentik", { callbackUrl: here }).catch(() => {
    leaving = false;
  });
  return true;
}
