/**
 * Active-route resolver. Picks the single best-matching nav href for the current
 * path: exact match wins over prefix; among prefixes the longest (most specific)
 * wins. The "+ '/'" boundary keeps /inventory/networks from matching
 * /inventory/network-acls, and lets /inventory/clusters/certificates highlight
 * its parent Clusters item. "/" only ever matches exactly (no "//" prefix).
 */
export function resolveActiveHref(
  hrefs: (string | undefined)[],
  pathname: string | null
): string | null {
  if (!pathname) return null;
  let best: string | null = null;
  let bestScore = -1;
  for (const href of hrefs) {
    if (!href) continue;
    let score = -1;
    if (pathname === href) score = href.length + 100_000;
    else if (pathname.startsWith(href + "/")) score = href.length;
    if (score > bestScore) {
      bestScore = score;
      best = href;
    }
  }
  return best;
}
