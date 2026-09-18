/**
 * Same-path URL writes that do not navigate.
 *
 * Filters, tabs and pickers keep their state in the query string, and every such write used to
 * go through `router.replace(url, { scroll: false })`. That is a navigation, and three things
 * come with it:
 *
 * - Next 15.5 routes a same-path, params-only replace through its aliased-prefetch path whenever
 *   a param-less prefetch entry for the page is alive (a sidebar link hovered in the last 30 s)
 *   and the segment has a `loading.tsx`. That path never sets `shouldScroll`, so the lazily
 *   refetched segment scrolls the document to the top under the open popover. Reproduced on the
 *   Overview, intermittently, on every build since the filter engine landed.
 * - Every keystroke of a debounced search was an RSC round trip to the server for a page whose
 *   data the client already holds.
 * - `replaceState` never adds a history entry, which is the contract the filter engine already
 *   promised: Back leaves the page, it does not walk the filter session.
 *
 * Next patches `window.history.replaceState` (app-router.js) to dispatch its restore action, so
 * `useSearchParams` and `usePathname` update on the next transition commit, with no fetch and no
 * scroll handling of its own. The patch only reads the state argument for its own markers, so a
 * fresh object is passed every time.
 *
 * important: the patch is installed after hydration. A write from a mount-time effect can run
 * before it exists and would then leave the router's own view of the URL stale, so effect-time
 * writers (legacy param translation, a default picked from data) stay on `router.replace`.
 * `scripts/checks/check-url-writes.mjs` holds that line. No `basePath` is configured; if one
 * ever is, `pathname` here must be prefixed with it.
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
 * The query string as the browser holds it right now. `useSearchParams` reports the rendered
 * value, which lags a write by one commit; a writer composing on top of another writer's change
 * in the same tick needs the live one.
 */
export function currentQuery(): URLSearchParams {
  if (typeof window === "undefined") return new URLSearchParams();
  return new URLSearchParams(window.location.search);
}
