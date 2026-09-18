"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  ROW_FOCUS_MARK_MS,
  clearRowFocus,
  getRowFocusSnapshot,
  isExpiredRowFocus,
  subscribeRowFocus,
  type RowFocusList,
} from "@/lib/row-focus";

export interface UseRowFocusOptions<T> {
  list: RowFocusList;
  /** Every row the page holds, before its filters. */
  rows: readonly T[];
  /** The rows after filter and sort: what the pager slices. */
  orderedRows: readonly T[];
  keyOf: (row: T) => string;
  pageSize: number;
  setPage: (page: number) => void;
  /** The list query's dataUpdatedAt. */
  dataUpdatedAt: number;
  /** What "Show it" does when a filter hides the row. Usually filters.clearAll. */
  clearFilters: () => void;
}

export interface RowFocusState {
  focusedKey: string | null;
  /** Set while the created row is in `rows` but not in `orderedRows`. */
  hidden: { key: string; label: string } | null;
  showHidden: () => void;
  /** Spread on the data row so the hook can find it and mark it. */
  rowProps: (key: string) => {
    "data-row-key": string;
    "data-row-focus"?: "true" | "false";
  };
}

/** The 700 ms the fade-out takes; matched by the primitive's `data-[row-focus]:duration-700`. */
const FADE_MS = 700;
// How long after the scroll the row is kept in view against layout that lands above it. The
// sections above a list (fleet status cards, alarm tiles) load on their own and push the
// list down after the row was centred; two seconds covers a slow fan-out and is short enough
// that an operator who scrolls away straight after is not fought.
const HOLD_IN_VIEW_MS = 2000;

/**
 * Bring a just-created row into view, once.
 *
 * important: a row is trusted only when the list was fetched after the intent was raised
 * (`dataUpdatedAt > intent.at`). Presence alone is not enough: delete-and-recreate under the
 * same name is routine, and the client cache or the server's 30 s list cache can still hold
 * the OLD row under that key, so presence would scroll to a row about to be replaced. A
 * stream upsert moves dataUpdatedAt too, so the instances placeholder resolving qualifies.
 */
export function useRowFocus<T>(opts: UseRowFocusOptions<T>): RowFocusState {
  const { list, rows, orderedRows, keyOf, pageSize, setPage, dataUpdatedAt, clearFilters } = opts;

  const intent = useSyncExternalStore(
    subscribeRowFocus,
    () => getRowFocusSnapshot()[list],
    () => getRowFocusSnapshot()[list],
  );

  const [focusedKey, setFocusedKey] = useState<string | null>(null);
  const [fading, setFading] = useState(false);
  const scrollPending = useRef<string | null>(null);
  const timers = useRef<number[]>([]);
  const holds = useRef<ResizeObserver[]>([]);
  const holdTimer = useRef<number | null>(null);

  const fresh = Boolean(intent && !isExpiredRowFocus(intent) && dataUpdatedAt > intent.at);
  const inRows = useMemo(
    () => (fresh && intent ? rows.some((row) => keyOf(row) === intent.key) : false),
    [fresh, intent, rows, keyOf],
  );
  const orderedIndex = useMemo(
    () => (fresh && intent ? orderedRows.findIndex((row) => keyOf(row) === intent.key) : -1),
    [fresh, intent, orderedRows, keyOf],
  );

  const hidden = fresh && intent && inRows && orderedIndex < 0
    ? { key: intent.key, label: intent.label }
    : null;

  // Consume the intent on the render that can act on it: jump to the holding page, mark the
  // row, and hand the scroll to the effect below, which runs once the row is in the DOM.
  useEffect(() => {
    if (!intent) return;
    if (isExpiredRowFocus(intent)) {
      clearRowFocus(list);
      return;
    }
    if (!fresh || orderedIndex < 0) return;
    setPage(Math.floor(orderedIndex / pageSize) + 1);
    scrollPending.current = intent.key;
    setFading(false);
    setFocusedKey(intent.key);
    clearRowFocus(list);
    for (const id of timers.current) window.clearTimeout(id);
    timers.current = [
      window.setTimeout(() => setFading(true), ROW_FOCUS_MARK_MS),
      window.setTimeout(() => {
        setFocusedKey(null);
        setFading(false);
      }, ROW_FOCUS_MARK_MS + FADE_MS),
    ];
  }, [intent, fresh, orderedIndex, pageSize, setPage, list]);

  // No dependency list on purpose: the row appears on the commit after setPage, and a
  // querySelector per render only while a scroll is pending is cheaper than guessing a delay.
  useEffect(() => {
    const key = scrollPending.current;
    if (!key) return;
    const row = document.querySelector<HTMLElement>(`[data-row-key="${CSS.escape(key)}"]`);
    if (!row) return;
    scrollPending.current = null;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    row.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    // Re-centre when the document grows above the row while the mark is fresh. A section
    // that finishes loading above the list moves the row out of the viewport after the
    // scroll landed, and the mark would then play off screen.
    if (typeof ResizeObserver === "undefined") return;
    const hold = new ResizeObserver(() => {
      const box = row.getBoundingClientRect();
      const inView = box.top >= 0 && box.bottom <= window.innerHeight;
      if (!inView) row.scrollIntoView({ block: "center", behavior: "auto" });
    });
    hold.observe(document.body);
    holdTimer.current = window.setTimeout(() => hold.disconnect(), HOLD_IN_VIEW_MS);
    holds.current.push(hold);
  });

  // An intent nobody could act on must not sit in the store forever behind a notice.
  useEffect(() => {
    if (!intent) return;
    const id = window.setTimeout(() => clearRowFocus(list), Math.max(0, intent.at + 5 * 60 * 1000 - Date.now()));
    return () => window.clearTimeout(id);
  }, [intent, list]);

  useEffect(
    () => () => {
      for (const id of timers.current) window.clearTimeout(id);
      for (const hold of holds.current) hold.disconnect();
      if (holdTimer.current !== null) window.clearTimeout(holdTimer.current);
    },
    [],
  );

  const showHidden = useCallback(() => clearFilters(), [clearFilters]);

  const rowProps = useCallback(
    (key: string) => {
      if (key !== focusedKey) return { "data-row-key": key };
      return { "data-row-key": key, "data-row-focus": (fading ? "false" : "true") as "true" | "false" };
    },
    [focusedKey, fading],
  );

  return { focusedKey, hidden, showHidden, rowProps };
}
