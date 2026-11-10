/**
 * Post-create focus intents.
 *
 * After a create, the new row lands wherever the sort, page size and active filter put it. This
 * store carries "bring this row into view" across the navigation from a create page or a dialog
 * close, and `useRowFocus` consumes it once the list holds the row. Kept outside React, like
 * pending-creates, so it survives `navigate()`.
 */

export type RowFocusList =
  | "instances"
  | "networks"
  | "network-acls"
  | "profiles"
  | "groups"
  | "images"
  | "iso-images"
  | "projects"
  | "my-keys";

export interface RowFocusIntent {
  list: RowFocusList;
  /** Exactly what the page's keyOf(row) returns for the created row. */
  key: string;
  /** What the notice calls it when a filter hides it. */
  label: string;
  /** The list must have been fetched after this instant before the row is trusted. */
  at: number;
}

/** An intent left unconsumed this long expires: the operator has moved on. */
export const ROW_FOCUS_TTL_MS = 5 * 60 * 1000;
/** How long the row stays marked. */
export const ROW_FOCUS_MARK_MS = 4000;

type Snapshot = Readonly<Partial<Record<RowFocusList, RowFocusIntent>>>;
type Listener = () => void;

let snapshot: Snapshot = {};
const listeners = new Set<Listener>();

function emit() {
  for (const listener of listeners) listener();
}

export function subscribeRowFocus(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getRowFocusSnapshot(): Snapshot {
  return snapshot;
}

/** One intent per list; a newer request replaces an older one the operator never reached. */
export function requestRowFocus(
  list: RowFocusList,
  key: string,
  label: string,
  opts: { at?: number } = {},
): void {
  snapshot = { ...snapshot, [list]: { list, key, label, at: opts.at ?? Date.now() } };
  emit();
}

export function clearRowFocus(list: RowFocusList): void {
  if (!snapshot[list]) return;
  const next = { ...snapshot };
  delete next[list];
  snapshot = next;
  emit();
}

export function isExpiredRowFocus(intent: RowFocusIntent, now: number = Date.now()): boolean {
  return now - intent.at > ROW_FOCUS_TTL_MS;
}

/** The projects grid keys a card by host and name; the create form has to build the same key. */
export function projectRowKey(host: string, name: string): string {
  return `${host}:${name}`;
}
