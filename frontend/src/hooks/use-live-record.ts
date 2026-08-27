"use client";

import { useEffect, useMemo, useRef } from "react";

export interface LiveRecord<T> {
  /** The row as the list last reported it, or the last one seen if it has since left. */
  record: T | null;
  /** The id is no longer in the list. What is on screen is the last known state. */
  isOrphaned: boolean;
}

/**
 * Re-derives a selected row from the live list on every render, so a dialog opened from a
 * table moves when the table moves.
 *
 * The pattern this replaces stored the clicked row in useState. React Query kept the list
 * fresh underneath and the dialog kept rendering the object captured at click time, so a
 * modal titled Memory sat on one number for as long as it stayed open.
 *
 * important: the fallback is keyed on the id, not a bare "last value". A bare fallback renders
 * the previous row's numbers under the new row's title when an operator opens a second row
 * while a poll is in flight, which is a worse failure than the freeze it replaces.
 *
 * note: `getId` belongs at module scope or behind useCallback. It is a real memo dependency
 * rather than being hidden behind a ref, because a ref would make a wrong accessor silently
 * permanent instead of merely re-running the find.
 */
export function useLiveRecord<T>(
  rows: readonly T[] | undefined,
  selectedId: string | null,
  getId: (row: T) => string,
): LiveRecord<T> {
  const found = useMemo(() => {
    if (!selectedId || !rows) return null;
    return rows.find((row) => getId(row) === selectedId) ?? null;
  }, [rows, selectedId, getId]);

  const lastSeen = useRef<{ id: string; row: T } | null>(null);
  useEffect(() => {
    if (found && selectedId) lastSeen.current = { id: selectedId, row: found };
  }, [found, selectedId]);

  const fallback = lastSeen.current?.id === selectedId ? lastSeen.current.row : null;

  return { record: found ?? fallback, isOrphaned: !found && fallback !== null };
}
