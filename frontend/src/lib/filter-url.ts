// Query-string codec for the non-enum filter kinds.
//
// One key per column, with a sigil inside the value:
//
//   enum    ?node=nuc2,blitz01          (unchanged, handled by table-filter.ts)
//   number  ?devices=n:2..8   ?size=n:4194304..   ?days=n:..30
//   date    ?created=d:last7d   ?expires=d:2026-09-01..   ?expires=d:~none
//   text    ?name=t:lxdbr
//
// important: a sigil inside the value rather than a suffixed key (`?devices.min=2`) is what
// keeps this small. The key stays exactly what keyFor(field) produces, so clearAll and the
// namespaced tabbed pages (`certificates.days`) keep working untouched, and there is no
// parse-on-the-last-dot rule to get wrong.
//
// note: collision with the enum format is structural, not statistical. serialiseFacet runs
// encodeURIComponent per value and a colon always escapes, so an enum serialisation can never
// produce a bare `n:`, `d:` or `t:` prefix. The same escaping is why a bare comma is a safe
// separator here: an encoded text query cannot contain one.
//
// note: this module is deliberately alias-free with no runtime imports, so the round-trip
// guard can compile it standalone and exercise it under node.

export const EMPTY_PARAM = "~none";
export const RANGE_SEPARATOR = "..";
export const NUMBER_SIGIL = "n:";
export const DATE_SIGIL = "d:";
export const TEXT_SIGIL = "t:";

/** Whether rows with no value for this column are hidden, kept, or the only ones kept. */
export type EmptyMode = "exclude" | "include" | "only";

export interface RangeState {
  min: number | null;
  max: number | null;
  empty: EmptyMode;
}

export type DatePresetId =
  | "today"
  | "yesterday"
  | "last7d"
  | "last30d"
  | "last90d"
  | "expired"
  | "next7d"
  | "next30d"
  | "next90d";

export interface DateState extends RangeState {
  /** Stored as an id, never as resolved bounds: `last7d` reloaded tomorrow means tomorrow. */
  preset: DatePresetId | null;
}

export interface TextState {
  query: string;
  empty: EmptyMode;
}

export const PAST_PRESETS: readonly DatePresetId[] = [
  "today",
  "yesterday",
  "last7d",
  "last30d",
  "last90d",
];
export const FUTURE_PRESETS: readonly DatePresetId[] = [
  "expired",
  "next7d",
  "next30d",
  "next90d",
];

export const DATE_PRESET_LABELS: Readonly<Record<DatePresetId, string>> = {
  today: "Today",
  yesterday: "Yesterday",
  last7d: "Last 7 days",
  last30d: "Last 30 days",
  last90d: "Last 90 days",
  expired: "Already past",
  next7d: "Next 7 days",
  next30d: "Next 30 days",
  next90d: "Next 90 days",
};

const DAY = 86_400_000;

/** Local midnight of the day containing `at`. */
export function dayStart(at: number): number {
  const d = new Date(at);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/**
 * Resolve a preset against a reference day.
 *
 * note: snapping to local midnight is what makes "last 7 days" mean seven whole calendar days
 * rather than a rolling 168 hours, and it keeps the reference stable across renders so the
 * memo chain does not recompute on every tick.
 */
export function resolvePreset(
  id: DatePresetId,
  now: number,
): { min: number | null; max: number | null } {
  const start = dayStart(now);
  const endOfToday = start + DAY - 1;
  switch (id) {
    case "today":
      return { min: start, max: endOfToday };
    case "yesterday":
      return { min: start - DAY, max: start - 1 };
    case "last7d":
      return { min: start - 6 * DAY, max: endOfToday };
    case "last30d":
      return { min: start - 29 * DAY, max: endOfToday };
    case "last90d":
      return { min: start - 89 * DAY, max: endOfToday };
    case "expired":
      return { min: null, max: start - 1 };
    case "next7d":
      return { min: start, max: endOfToday + 6 * DAY };
    case "next30d":
      return { min: start, max: endOfToday + 29 * DAY };
    case "next90d":
      return { min: start, max: endOfToday + 89 * DAY };
  }
}

export const INACTIVE_RANGE: RangeState = { min: null, max: null, empty: "exclude" };
export const INACTIVE_DATE: DateState = { min: null, max: null, empty: "exclude", preset: null };
export const INACTIVE_TEXT: TextState = { query: "", empty: "exclude" };

export function isRangeActive(s: RangeState): boolean {
  return s.min !== null || s.max !== null || s.empty !== "exclude";
}
export function isDateActive(s: DateState): boolean {
  return s.preset !== null || isRangeActive(s);
}
export function isTextActive(s: TextState): boolean {
  return s.query !== "" || s.empty !== "exclude";
}

// ---------------------------------------------------------------------------- predicates

export function matchesRange(value: number | null | undefined, s: RangeState): boolean {
  if (!isRangeActive(s)) return true;
  const missing = value === null || value === undefined || Number.isNaN(value);
  if (s.empty === "only") return missing;
  if (missing) return s.empty === "include";
  if (s.min !== null && (value as number) < s.min) return false;
  if (s.max !== null && (value as number) > s.max) return false;
  return true;
}

export function matchesText(
  value: string | null | undefined | readonly (string | null | undefined)[],
  s: TextState,
): boolean {
  if (!isTextActive(s)) return true;
  const parts = (Array.isArray(value) ? value : [value]).filter(
    (v): v is string => typeof v === "string" && v !== "",
  );
  if (s.empty === "only") return parts.length === 0;
  if (parts.length === 0) return s.empty === "include";
  if (!s.query) return true;
  const needle = s.query.toLowerCase();
  return parts.some((p) => p.toLowerCase().includes(needle));
}

// ---------------------------------------------------------------------------- serialise

function withEmpty(body: string, empty: EmptyMode): string {
  if (empty === "only") return EMPTY_PARAM;
  return empty === "include" ? `${body},${EMPTY_PARAM}` : body;
}

function num(value: number | null): string {
  return value === null ? "" : String(value);
}

export function serialiseRange(s: RangeState): string | null {
  if (!isRangeActive(s)) return null;
  if (s.empty === "only") return `${NUMBER_SIGIL}${EMPTY_PARAM}`;
  const body = `${num(s.min)}${RANGE_SEPARATOR}${num(s.max)}`;
  return `${NUMBER_SIGIL}${withEmpty(body, s.empty)}`;
}

/** Local calendar day, so a shared link means the same days rather than the same instants. */
export function toDayString(at: number): string {
  const d = new Date(at);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function serialiseDate(s: DateState): string | null {
  if (!isDateActive(s)) return null;
  if (s.preset) return `${DATE_SIGIL}${withEmpty(s.preset, s.empty)}`;
  if (s.empty === "only") return `${DATE_SIGIL}${EMPTY_PARAM}`;
  const from = s.min === null ? "" : toDayString(s.min);
  const to = s.max === null ? "" : toDayString(s.max);
  return `${DATE_SIGIL}${withEmpty(`${from}${RANGE_SEPARATOR}${to}`, s.empty)}`;
}

/**
 * important: the tilde is escaped by hand. encodeURIComponent leaves `~` alone, so a query
 * that is literally "~none" would serialise to a value indistinguishable from the empty
 * sentinel and parse back as "only rows with no value". The round-trip guard caught exactly
 * that, which is what it exists for.
 */
function encodeQuery(query: string): string {
  return encodeURIComponent(query).replace(/~/g, "%7E");
}

export function serialiseText(s: TextState): string | null {
  if (!isTextActive(s)) return null;
  if (s.empty === "only") return `${TEXT_SIGIL}${EMPTY_PARAM}`;
  return `${TEXT_SIGIL}${withEmpty(encodeQuery(s.query), s.empty)}`;
}

// ---------------------------------------------------------------------------- parse
//
// important: every parser is total. These strings come from the address bar, so malformed
// input degrades to inactive and never throws, and nothing here rewrites the URL.

function splitEmpty(body: string): { body: string; empty: EmptyMode } {
  if (body === EMPTY_PARAM) return { body: "", empty: "only" };
  if (body.endsWith(`,${EMPTY_PARAM}`)) {
    return { body: body.slice(0, -(EMPTY_PARAM.length + 1)), empty: "include" };
  }
  return { body, empty: "exclude" };
}

function toNumber(raw: string, integer: boolean): number | null {
  if (raw === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return integer ? Math.trunc(n) : n;
}

export function parseRange(raw: string | null, integer = false): RangeState {
  if (!raw) return INACTIVE_RANGE;
  // A bare number is accepted so a link written when the column was an enum still works, and
  // matches wider rather than hiding a row it used to show.
  const payload = raw.startsWith(NUMBER_SIGIL) ? raw.slice(NUMBER_SIGIL.length) : raw;
  const { body, empty } = splitEmpty(payload);
  if (empty === "only") return { min: null, max: null, empty };

  if (!body.includes(RANGE_SEPARATOR)) {
    const exact = toNumber(body, integer);
    if (exact === null) return INACTIVE_RANGE;
    return { min: exact, max: exact, empty };
  }
  const at = body.indexOf(RANGE_SEPARATOR);
  let min = toNumber(body.slice(0, at), integer);
  let max = toNumber(body.slice(at + RANGE_SEPARATOR.length), integer);
  // A reversed range is swapped rather than rejected: an unremovable validation error inside
  // a popover is worse than a range that means what the numbers say.
  if (min !== null && max !== null && min > max) [min, max] = [max, min];
  const state = { min, max, empty };
  return isRangeActive(state) ? state : INACTIVE_RANGE;
}

const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function parseDayStart(raw: string): number | null {
  if (!DAY_PATTERN.test(raw)) return null;
  const [y, m, d] = raw.split("-").map(Number);
  const at = new Date(y, m - 1, d, 0, 0, 0, 0);
  // Rejects 2026-13-45: the Date constructor rolls over, so the round trip disagrees.
  if (at.getFullYear() !== y || at.getMonth() !== m - 1 || at.getDate() !== d) return null;
  return at.getTime();
}

function parseDayEnd(raw: string): number | null {
  const start = parseDayStart(raw);
  return start === null ? null : start + DAY - 1;
}

export function parseDate(
  raw: string | null,
  presets: readonly DatePresetId[] = [...PAST_PRESETS, ...FUTURE_PRESETS],
): DateState {
  if (!raw) return INACTIVE_DATE;
  const payload = raw.startsWith(DATE_SIGIL) ? raw.slice(DATE_SIGIL.length) : raw;
  const { body, empty } = splitEmpty(payload);
  if (empty === "only") return { min: null, max: null, empty, preset: null };

  if (presets.includes(body as DatePresetId)) {
    return { min: null, max: null, empty, preset: body as DatePresetId };
  }

  if (!body.includes(RANGE_SEPARATOR)) {
    const only = parseDayStart(body);
    if (only === null) return INACTIVE_DATE;
    return { min: only, max: only + DAY - 1, empty, preset: null };
  }
  const at = body.indexOf(RANGE_SEPARATOR);
  let min = parseDayStart(body.slice(0, at));
  let max = parseDayEnd(body.slice(at + RANGE_SEPARATOR.length));
  if (min !== null && max !== null && min > max) [min, max] = [max, min];
  const state: DateState = { min, max, empty, preset: null };
  return isDateActive(state) ? state : INACTIVE_DATE;
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function parseText(raw: string | null): TextState {
  if (!raw) return INACTIVE_TEXT;
  const payload = raw.startsWith(TEXT_SIGIL) ? raw.slice(TEXT_SIGIL.length) : raw;
  const { body, empty } = splitEmpty(payload);
  if (empty === "only") return { query: "", empty };
  const state = { query: safeDecode(body), empty };
  return isTextActive(state) ? state : INACTIVE_TEXT;
}

/** Bounds a date state actually filters by, resolving a preset against `now`. */
export function resolveDateBounds(
  s: DateState,
  now: number,
): { min: number | null; max: number | null } {
  return s.preset ? resolvePreset(s.preset, now) : { min: s.min, max: s.max };
}

/**
 * Bucket for a row whose facet value is missing or empty.
 *
 * note: a control character, so it cannot collide with a real node, project or type name.
 */
export const EMPTY_FACET = "\u0000none";
export const EMPTY_FACET_PARAM = "~none";

/**
 * Serialise selected facet values into one query-string value.
 *
 * important: the tilde is escaped by hand, the same reason encodeQuery does it above.
 * encodeURIComponent leaves `~` alone, so a facet value that is literally the empty sentinel
 * would serialise to something indistinguishable from it and parse back as "only rows with no
 * value". Node and project names come from the cluster, not from us.
 *
 * note: this lives here rather than beside the facet types in table-filter.ts so the round-trip
 * guard can compile and exercise it. table-filter.ts imports the whole app graph through its
 * `@/` aliases; this module is deliberately alias-free and import-free. The guard used to test an
 * inline re-implementation of this encoding, which is how the missing tilde escape survived.
 */
export function serialiseFacet(values: readonly string[]): string {
  return values
    .map((value) =>
      value === EMPTY_FACET
        ? EMPTY_FACET_PARAM
        : encodeURIComponent(value).replace(/~/g, "%7E"),
    )
    .join(",");
}

export function parseFacet(raw: string | null): string[] {
  if (!raw) return [];
  return raw
    .split(",")
    .filter(Boolean)
    .map((part) => {
      if (part === EMPTY_FACET_PARAM) return EMPTY_FACET;
      try {
        return decodeURIComponent(part);
      } catch {
        return part;
      }
    });
}
