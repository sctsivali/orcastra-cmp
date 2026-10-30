"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const OVERSCAN = 10;

/**
 * Which rows of a long table are in the DOM.
 *
 * important: fixed height only, which is why this is 40 lines and not a dependency. Pagination was
 * the alternative and it breaks what a file list is for: Shift-selecting a range, arrow keys, and
 * type-ahead all cross page boundaries. `content-visibility` does not apply to a `<tr>`.
 *
 * note: the table owns its scroll container (ui/table.tsx), found from the table element.
 */
export function useFixedRowWindow(rowHeight: number, rowCount: number, pinned: number | null) {
  const tableRef = useRef<HTMLTableElement>(null);
  const scrollerRef = useRef<HTMLElement | null>(null);
  const [view, setView] = useState({ top: 0, height: 600 });

  useEffect(() => {
    const scroller = tableRef.current?.closest<HTMLElement>('[data-slot="table-container"]') ?? null;
    scrollerRef.current = scroller;
    if (!scroller) return;
    const update = () => setView({ top: scroller.scrollTop, height: scroller.clientHeight || 600 });
    update();
    scroller.addEventListener("scroll", update, { passive: true });
    const observer = new ResizeObserver(update);
    observer.observe(scroller);
    return () => {
      scroller.removeEventListener("scroll", update);
      observer.disconnect();
    };
  }, []);

  const first = Math.max(0, Math.floor(view.top / rowHeight) - OVERSCAN);
  const last = Math.min(rowCount, Math.ceil((view.top + view.height) / rowHeight) + OVERSCAN);
  // The focused row stays mounted, or keyboard focus would drop to the body when it scrolled away.
  const pinnedOutside = pinned !== null && pinned >= 0 && pinned < rowCount && (pinned < first || pinned >= last);

  const scrollToRow = useCallback(
    (index: number) => {
      const scroller = scrollerRef.current;
      if (!scroller) return;
      const headerHeight = 32;
      const top = index * rowHeight;
      const bottom = top + rowHeight + headerHeight;
      if (top < scroller.scrollTop) scroller.scrollTop = top;
      else if (bottom > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = bottom - scroller.clientHeight;
    },
    [rowHeight],
  );

  return {
    tableRef,
    first,
    last,
    topSpacer: first * rowHeight,
    bottomSpacer: Math.max(0, (rowCount - last) * rowHeight),
    pinnedOutside,
    scrollToRow,
  };
}
