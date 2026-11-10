"use client";

import * as React from "react";
import { currentQuery, goBack, pushQuery, replaceQuery } from "@/lib/url-state";

type QueryPatch = Record<string, string | null>;

/**
 * Query-string dialogs that the browser's Back closes.
 *
 * A dialog opened from the page pushes its own history entry, so Back closes it, and Close steps
 * back over that entry rather than stacking a second copy of the page. A dialog the page was
 * loaded with has no entry of ours, so its Close replaces the URL. Moving between dialogs replaces
 * the entry, so one Back always reaches the page.
 */
export function useOverlayHistory(
  pathname: string,
  overlayOpen: boolean,
  patchQuery: (patch: QueryPatch) => void,
) {
  const entryRef = React.useRef(false);
  React.useEffect(() => {
    if (!overlayOpen) entryRef.current = false;
  }, [overlayOpen]);

  const openOverlay = React.useCallback(
    (patch: QueryPatch) => {
      const params = currentQuery();
      for (const [key, value] of Object.entries(patch)) {
        if (value) params.set(key, value);
        else params.delete(key);
      }
      if (entryRef.current) {
        replaceQuery(pathname, params);
      } else {
        entryRef.current = true;
        pushQuery(pathname, params);
      }
    },
    [pathname],
  );

  const closeOverlay = React.useCallback(
    (patch: QueryPatch) => {
      if (!entryRef.current) return patchQuery(patch);
      entryRef.current = false;
      goBack();
    },
    [patchQuery],
  );

  return { openOverlay, closeOverlay };
}
