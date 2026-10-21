"use client";

import * as React from "react";
import { currentQuery, goBack, pushQuery, replaceQuery } from "@/lib/url-state";

type QueryPatch = Record<string, string | null>;

/**
 * Query-string dialogs that the browser's Back closes.
 *
 * important: a dialog opened from the page is its own history entry, so Back closes it instead of
 * leaving the page, and Close steps back over that entry rather than stacking a second copy of the
 * page under it. A dialog the page was loaded with (a link from another page) has no entry of
 * ours, so its Close replaces the URL and Back leaves the page as before. Moving from one dialog to
 * another while one is open replaces the entry, so one Back always reaches the page.
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
