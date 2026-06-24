"use client";

import { useEffect } from "react";

// Apply URL query params to local filter state ONCE on mount, so a deep link
// like /inventory/instances?q=nginx&host=lxd-1 lands on a pre-filtered table
// (reuses each page's existing search/filter state). Read post-hydration to
// avoid SSR/CSR mismatch, and only once so it never clobbers later user edits.
export function useInitialSearchParams(handlers: Record<string, (value: string) => void>) {
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    for (const key of Object.keys(handlers)) {
      const value = params.get(key);
      if (value) handlers[key](value);
    }
    // run once; useState setters are stable
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
