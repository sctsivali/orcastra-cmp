/**
 * Same-path URL writes that do not navigate.
 *
 * On Next 15.5 a same-path, params-only `router.replace` can take the aliased-prefetch path (a
 * param-less prefetch entry alive, a `loading.tsx` on the segment), which never sets
 * `shouldScroll`: the page jumps to the top under an open popover, and each keystroke costs an
 * RSC round trip. Next patches `window.history.replaceState` (app-router.js) to update
 * `useSearchParams` and `usePathname` on the next commit with no fetch or scroll; the patch reads
 * the state argument for its own markers, so a fresh object is passed every time. `replaceState`
 * adds no history entry, so Back leaves the page instead of walking the filter session.
 *
 * important: the patch is installed after hydration. A mount-time effect write can run before it
 * and leave the router's URL stale, so effect-time writers stay on `router.replace`
 * (scripts/checks/check-url-writes.mjs). No `basePath` is configured; adding one means prefixing
 * `pathname` here.
 */

export function replaceUrl(url: string): void {
  if (typeof window === "undefined" || !url) return;
  window.history.replaceState({}, "", url);
}

/** Replace the query string of `pathname` with `params`; an empty string clears it. */
export function replaceQuery(pathname: string, params: URLSearchParams | string): void {
  const query = typeof params === "string" ? params : params.toString();
  replaceUrl(query ? `${pathname}?${query}` : pathname);
}

/**
 * Push `pathname?params` as a new history entry, for state Back should return from: an overlay
 * opened from the page (a dialog), or a settings section (?section=), which Back walks the way it
 * walks the Settings tabs. Not for filter state, which Back should walk past.
 */
export function pushQuery(pathname: string, params: URLSearchParams | string): void {
  if (typeof window === "undefined") return;
  const query = typeof params === "string" ? params : params.toString();
  window.history.pushState({}, "", query ? `${pathname}?${query}` : pathname);
}

/** Leave the current history entry the way the browser's Back button does. */
export function goBack(): void {
  if (typeof window !== "undefined") window.history.back();
}

/**
 * The query string as the browser holds it right now. `useSearchParams` reports the rendered
 * value, which lags a write by one commit; a writer composing on top of another writer's change
 * in the same tick needs the live one.
 */
export function currentQuery(): URLSearchParams {
  if (typeof window === "undefined") return new URLSearchParams();
  return new URLSearchParams(window.location.search);
}
