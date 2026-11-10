"use client";

import { useCallback, useMemo } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { currentQuery, pushQuery } from "@/lib/url-state";

/** The query key a settings tab keeps its section in, ex; /settings/integrations?section=organizations. */
export const SECTION_PARAM = "section";

/**
 * The section of a settings tab, read from `?section=` and written back with a history entry.
 *
 * important: a query parameter, not a nested path. The middleware answers 404 for any
 * /settings/<tab>/<more>, and the default section carries no parameter, so
 * /settings/integrations stays the canonical URL.
 *
 * An unknown or repeated value falls back to the default rather than being echoed, and the URL
 * is never rewritten on mount: a mount-time write would land before Next's history patch is
 * installed (lib/url-state.ts) and leave the router's view of the URL stale.
 */
export function useSettingsSection<S extends string>(sections: readonly S[], fallback: S) {
  const params = useSearchParams();
  const pathname = usePathname();
  const raw = params.get(SECTION_PARAM);
  const section: S = raw !== null && (sections as readonly string[]).includes(raw) ? (raw as S) : fallback;

  const queryFor = useCallback(
    (next: S, base: URLSearchParams) => {
      const query = new URLSearchParams(base);
      if (next === fallback) query.delete(SECTION_PARAM);
      else query.set(SECTION_PARAM, next);
      return query;
    },
    [fallback],
  );

  // Rendered hrefs come from the rendered params, so a server and client render agree.
  const paramsString = params.toString();
  const hrefFor = useMemo(() => {
    const base = new URLSearchParams(paramsString);
    return (next: S) => {
      const query = queryFor(next, base).toString();
      return query ? `${pathname}?${query}` : pathname;
    };
  }, [paramsString, pathname, queryFor]);

  // A Back press walks sections, the way the outer Settings tabs push history.
  const select = useCallback(
    (next: S) => {
      if (next === section) return;
      pushQuery(pathname, queryFor(next, currentQuery()));
    },
    [pathname, queryFor, section],
  );

  return { section, select, hrefFor };
}
