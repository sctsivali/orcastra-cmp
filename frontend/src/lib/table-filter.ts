// Shared client-side table filtering primitives.
//
// The companion to lib/sort.ts. Every dashboard table fetches its full dataset, so
// filtering happens in the browser next to sorting and pagination. A page describes each
// column once and this module derives the sort accessors, the facet option lists and the
// search predicate from that one description.
//
// note: every facet is an enum. Numerics and timestamps are deliberately not faceted (a
// value list over byte sizes means nothing), and two-value dimensions such as "Managed" or
// "In use" read better as a named pair than as a boolean widget. One kind keeps the engine
// and the URL format small.

import type { SortDirection } from "@/lib/sort";
import {
  matchesRange,
  matchesText,
  resolveDateBounds,
  type DatePresetId,
  type DateState,
  type RangeState,
  type TextState,
} from "@/lib/filter-url";
import { FUTURE_PRESETS, PAST_PRESETS, EMPTY_FACET } from "@/lib/filter-url";

/**
 * Bucket for a row whose facet value is missing or empty.
 *
 * note: a control character, so it cannot collide with a real node, project or type name.
 */
export { EMPTY_FACET, EMPTY_FACET_PARAM, serialiseFacet, parseFacet } from "@/lib/filter-url";

/**
 * Query keys the toolbar owns. A column field may not collide with one, because the
 * collision would silently corrupt a shared link rather than fail loudly.
 *
 * note: deliberately narrow. `q`, `show` and `tab` are written today, and `sort`/`dir` are
 * held for the day sorting becomes URL-backed. `page` and `size` are NOT reserved: the page
 * number is deliberately kept out of the URL, and `size` is an ordinary column name on both
 * images and storage pools. Reserving a key nothing writes only blocks honest columns.
 */
export const RESERVED_QUERY_KEYS = ["q", "sort", "dir", "show", "tab"] as const;

export type FilterKind = "enum" | "number" | "date" | "text";

/** Chooses how a bound is formatted and typed. Never affects the stored value. */
export type NumberUnit = "count" | "bytes" | "percent" | "seconds" | "days" | "bps";

export interface NumberPreset {
  label: string;
  min: number | null;
  max: number | null;
}

interface KindSpecBase {
  primary?: boolean;
  role?: FilterRole;
  emptyLabel?: string;
}

export interface NumberFilterOptions extends KindSpecBase {
  unit?: NumberUnit;
  /** Defaults to true for counts and days. */
  integer?: boolean;
  /** One-click shortcuts. On a count column this is where most of the value is. */
  presets?: readonly NumberPreset[];
}

export interface DateFilterOptions extends KindSpecBase {
  /** Chooses the default preset set. `past` fits created, `future` fits expires. */
  horizon?: "past" | "future";
  presets?: readonly DatePresetId[];
}

export interface TextFilterOptions extends KindSpecBase {
  placeholder?: string;
}

/**
 * Reuse the column's own `sort` accessor as the filter value.
 *
 * important: this saves the duplicate lambda, never the decision. The kind is always named,
 * because a sort accessor's return type does not determine it: `size ?? 0`,
 * `is_expired ? 1 : 0` and `new Date(created_at).getTime()` are all `(row) => number` and want
 * a range, an enum and a date respectively.
 */
export interface FromSortMarker {
  readonly __fromSort: true;
  readonly kind: Exclude<FilterKind, "enum">;
  readonly options: NumberFilterOptions | DateFilterOptions | TextFilterOptions;
}

export function fromSort(kind: "number", options?: NumberFilterOptions): FromSortMarker;
export function fromSort(kind: "date", options?: DateFilterOptions): FromSortMarker;
export function fromSort(kind: "text", options?: TextFilterOptions): FromSortMarker;
export function fromSort(
  kind: Exclude<FilterKind, "enum">,
  options: NumberFilterOptions | DateFilterOptions | TextFilterOptions = {},
): FromSortMarker {
  return { __fromSort: true, kind, options };
}

export type FilterDeclaration<T, C> =
  | FromSortMarker
  | { kind: "number"; value: (row: T, context: C) => number | null | undefined } & NumberFilterOptions
  | { kind: "date"; value: (row: T, context: C) => number | null | undefined } & DateFilterOptions
  | { kind: "text"; value: (row: T, context: C) => string | null | undefined | readonly (string | null | undefined)[] } & TextFilterOptions;

export type ResolvedFilter<T, C> =
  | { kind: "enum"; value: (row: T, context: C) => FacetValue; spec: FacetSpec }
  | { kind: "number"; value: (row: T, context: C) => number | null | undefined; options: Required<Pick<NumberFilterOptions, "unit" | "integer">> & NumberFilterOptions }
  | { kind: "date"; value: (row: T, context: C) => number | null | undefined; options: DateFilterOptions & { presets: readonly DatePresetId[] } }
  | { kind: "text"; value: (row: T, context: C) => string | null | undefined | readonly (string | null | undefined)[]; options: TextFilterOptions };

export type FacetValue =
  | string
  | number
  | boolean
  | null
  | undefined
  | readonly (string | number)[];

/**
 * A dimension this column represents across the whole console, independent of what this table
 * happens to call it.
 *
 * important: every rule keys off the role, never off the field name, because no name-based rule
 * can work here. Node is spelled `node` on 13 tables, `host` on two and `cluster` on four, and
 * `clusters.cluster` is the row's own identity while `certificates.cluster` is the node
 * dimension. Project is `project` on four tables and `projects` on five.
 */
export type FilterRole = "node" | "project";

/** Roles pinned to the toolbar wherever the column exists, whatever the option count. */
export const PINNED_ROLES: readonly FilterRole[] = ["node", "project"];

/** Non-pinned primaries rendered at rest before the rest fall behind "Add filter". */
export const MAX_RESTING_FACETS = 2;

export interface FacetSpec {
  /** Rendered on the toolbar at rest. Everything else sits behind "Add filter". */
  primary?: boolean;
  /**
   * Marks this column as the node or project dimension. A pinned role is always rendered and
   * is exempt from the two-value threshold, because these are the dimensions the product is
   * organised around and their presence should not depend on today's fleet.
   */
  role?: FilterRole;
  /** Display transform for an option label. Never applied to the stored value. */
  format?: (value: string) => string;
  /** Label for the empty bucket. */
  emptyLabel?: string;
  /** Fixed option order. Otherwise count descending, then label ascending. */
  order?: readonly string[];
  /** Search within the option list appears past this many options. */
  searchThreshold?: number;
}

export interface TableColumn<T, F extends string = string, C = void> {
  /** Sort field and URL key. Never change it once shipped: it travels in shared links. */
  field: F;
  label: string;
  /**
   * Raw comparable value for sorting. Never a formatted string, or the comparator starts
   * ordering the display text instead of the value.
   */
  sort?: (row: T) => unknown;
  /** Present means filterable. An array return means the row holds several values at once. */
  facet?: (row: T, context: C) => FacetValue;
  facetSpec?: FacetSpec;
  /**
   * The general form, for a column whose values are not a bounded vocabulary. Mutually
   * exclusive with `facet`: declaring both throws.
   */
  filter?: FilterDeclaration<T, C>;
  /** Present means this column's text is searched. */
  search?: (row: T) => string | null | undefined;
}

export interface TableDefinition<T, F extends string = string, C = void> {
  /** URL namespace. Two tables on one page need distinct ids. */
  id?: string;
  columns: readonly TableColumn<T, F, C>[];
  searchPlaceholder: string;
  /** Text matched by search that is not a rendered column, ex; a description. */
  searchExtra?: (row: T) => (string | null | undefined)[];
  defaultSort: { field: F; direction?: SortDirection };
  /** Query keys this page owns for something else, ex; the cluster selector on members. */
  reservedParams?: readonly string[];
}

export interface CompiledTable<T, F extends string = string, C = void>
  extends TableDefinition<T, F, C> {
  readonly sortAccessors: Readonly<Record<F, (row: T) => unknown>>;
  readonly byField: Readonly<Record<F, TableColumn<T, F, C>>>;
  readonly facetFields: readonly F[];
  readonly primaryFacetFields: readonly F[];
  readonly searchFields: readonly F[];
  /** Fields carrying a pinned role. Derived, never hand-listed. */
  readonly pinnedFields: readonly F[];
  /** The one shape the engine reads, whichever spelling the page authored. */
  readonly filterSpecs: Readonly<Record<F, ResolvedFilter<T, C>>>;
  /** The enum subset, which is the only kind with an option list to build. */
  readonly enumFields: readonly F[];
}

/**
 * Two-step call so T and C are given while F is inferred from the column literals.
 *
 * important: call this at module scope. The result is frozen and stable, which is what
 * useTableSort's accessor map has always needed and previously asked each page to remember.
 */
export function defineTable<T, C = void>() {
  return <const F extends string>(
    definition: TableDefinition<T, F, C>,
  ): CompiledTable<T, F, C> => {
    const sortAccessors = {} as Record<F, (row: T) => unknown>;
    const byField = {} as Record<F, TableColumn<T, F, C>>;
    const facetFields: F[] = [];
    const primaryFacetFields: F[] = [];
    const searchFields: F[] = [];
    const pinnedFields: F[] = [];
    const filterSpecs = {} as Record<F, ResolvedFilter<T, C>>;
    const enumFields: F[] = [];
    const seenRoles = new Map<FilterRole, F>();
    const reserved = new Set(definition.reservedParams ?? []);

    for (const column of definition.columns) {
      if ((RESERVED_QUERY_KEYS as readonly string[]).includes(column.field)) {
        throw new Error(
          `Table column "${column.field}" collides with a reserved query key. Rename the field.`,
        );
      }
      // important: reservedParams was declared and never read until now, so a page that owns a
      // query key for something else (the cluster selector on clustering members) was protected
      // by nothing but the accidental absence of a column with that name.
      if (reserved.has(column.field)) {
        throw new Error(
          `Table column "${column.field}" collides with a reserved query param on this page. Rename the field.`,
        );
      }

      if (column.facet && column.filter) {
        throw new Error(
          `Table column "${column.field}" declares both facet and filter. Pick one spelling.`,
        );
      }

      const declared = column.filter;
      const role = column.facetSpec?.role ?? (declared && "role" in declared ? declared.role : undefined);
      if (role) {
        const claimed = seenRoles.get(role);
        if (claimed) {
          throw new Error(
            `Table declares role "${role}" on both "${claimed}" and "${column.field}". A page has one node dimension and one project dimension.`,
          );
        }
        seenRoles.set(role, column.field);
        if (PINNED_ROLES.includes(role)) pinnedFields.push(column.field);
      }

      byField[column.field] = column;
      if (column.sort) sortAccessors[column.field] = column.sort;

      if (column.facet) {
        facetFields.push(column.field);
        enumFields.push(column.field);
        filterSpecs[column.field] = {
          kind: "enum",
          value: column.facet,
          spec: column.facetSpec ?? {},
        };
        if (column.facetSpec?.primary) primaryFacetFields.push(column.field);
      } else if (declared) {
        const resolved = resolveDeclaration<T, F, C>(column, declared);
        facetFields.push(column.field);
        filterSpecs[column.field] = resolved;
        const primary = "primary" in declared ? declared.primary : undefined;
        if (primary) primaryFacetFields.push(column.field);
      }

      if (column.search) searchFields.push(column.field);
    }

    return Object.freeze({
      ...definition,
      sortAccessors: Object.freeze(sortAccessors),
      byField: Object.freeze(byField),
      facetFields: Object.freeze(facetFields),
      primaryFacetFields: Object.freeze(primaryFacetFields),
      searchFields: Object.freeze(searchFields),
      pinnedFields: Object.freeze(pinnedFields),
      filterSpecs: Object.freeze(filterSpecs),
      enumFields: Object.freeze(enumFields),
    });
  };
}

/** Normalise one row's facet value to the list of buckets it belongs to. */
export function facetValuesOf(value: FacetValue): string[] {
  if (Array.isArray(value)) {
    const values = value.filter((entry) => entry !== null && entry !== undefined && entry !== "");
    return values.length ? values.map(String) : [EMPTY_FACET];
  }
  if (value === null || value === undefined || value === "") return [EMPTY_FACET];
  return [String(value)];
}

/** A row matches a facet when any of its values is selected. Selecting nothing matches all. */
export function matchesFacet(value: FacetValue, selected: readonly string[]): boolean {
  if (selected.length === 0) return true;
  return facetValuesOf(value).some((entry) => selected.includes(entry));
}

export interface FacetOption {
  value: string;
  label: string;
  count: number;
}

export function optionLabel(value: string, spec?: FacetSpec): string {
  if (value === EMPTY_FACET) return spec?.emptyLabel ?? "(none)";
  return spec?.format ? spec.format(value) : value;
}

/**
 * Build the option list for one facet over the given rows.
 *
 * important: callers pass the rows already narrowed by the search and by every OTHER
 * facet, never the raw set. A count taken over the raw set offers options that yield zero
 * rows once another filter is on, which reads as a bug.
 */
export function buildFacetOptions<T, F extends string, C>(
  column: TableColumn<T, F, C>,
  rows: readonly T[],
  context: C,
): FacetOption[] {
  if (!column.facet) return [];
  const counts = new Map<string, number>();
  for (const row of rows) {
    for (const value of facetValuesOf(column.facet(row, context))) {
      counts.set(value, (counts.get(value) ?? 0) + 1);
    }
  }

  const spec = column.facetSpec;
  const options = Array.from(counts.entries()).map(([value, count]) => ({
    value,
    label: optionLabel(value, spec),
    count,
  }));

  if (spec?.order) {
    const rank = new Map(spec.order.map((value, position) => [value, position]));
    const last = spec.order.length;
    options.sort(
      (a, b) =>
        (rank.get(a.value) ?? last) - (rank.get(b.value) ?? last) ||
        a.label.localeCompare(b.label),
    );
    return options;
  }

  // The empty bucket always sorts last: it is the absence of a value, not a value.
  options.sort((a, b) => {
    if (a.value === EMPTY_FACET) return 1;
    if (b.value === EMPTY_FACET) return -1;
    return b.count - a.count || a.label.localeCompare(b.label);
  });
  return options;
}

export function rowMatchesSearch<T, F extends string, C>(
  table: CompiledTable<T, F, C>,
  row: T,
  query: string,
): boolean {
  if (!query) return true;
  const needle = query.toLowerCase();
  for (const field of table.searchFields) {
    const text = table.byField[field].search?.(row);
    if (text && text.toLowerCase().includes(needle)) return true;
  }
  for (const text of table.searchExtra?.(row) ?? []) {
    if (text && text.toLowerCase().includes(needle)) return true;
  }
  return false;
}


/** Normalise the authored declaration into the one shape the engine reads. */
function resolveDeclaration<T, F extends string, C>(
  column: TableColumn<T, F, C>,
  declared: FilterDeclaration<T, C>,
): ResolvedFilter<T, C> {
  const isMarker = "__fromSort" in declared;
  const kind = declared.kind;

  if (isMarker && !column.sort) {
    throw new Error(
      `Table column "${column.field}" uses fromSort() but declares no sort accessor.`,
    );
  }
  const accessor = isMarker
    ? (row: T) => column.sort!(row)
    : (declared as { value: (row: T, context: C) => unknown }).value;
  const options = (isMarker ? declared.options : declared) as Record<string, unknown>;

  if (kind === "number") {
    const unit = (options.unit as NumberUnit) ?? "count";
    return {
      kind: "number",
      value: accessor as (row: T, context: C) => number | null | undefined,
      options: {
        ...(options as NumberFilterOptions),
        unit,
        integer: (options.integer as boolean) ?? (unit === "count" || unit === "days"),
      },
    };
  }
  if (kind === "date") {
    const horizon = (options.horizon as "past" | "future") ?? "past";
    return {
      kind: "date",
      value: accessor as (row: T, context: C) => number | null | undefined,
      options: {
        ...(options as DateFilterOptions),
        horizon,
        presets:
          (options.presets as readonly DatePresetId[]) ??
          (horizon === "future" ? FUTURE_PRESETS : PAST_PRESETS),
      },
    };
  }
  return {
    kind: "text",
    value: accessor as (row: T, context: C) => string | null | undefined | readonly (string | null | undefined)[],
    options: options as TextFilterOptions,
  };
}

/** The state a filter of each kind carries. Enum keeps its array of selected values. */
export type FilterState =
  | { kind: "enum"; values: string[] }
  | { kind: "number"; state: RangeState }
  | { kind: "date"; state: DateState }
  | { kind: "text"; state: TextState };

/**
 * Does this row pass this filter?
 *
 * note: `now` is passed in rather than read here, so a date preset resolves against one stable
 * reference for the whole pass instead of drifting mid-loop.
 */
export function matchesFilter<T, C>(
  spec: ResolvedFilter<T, C>,
  state: FilterState,
  row: T,
  context: C,
  now: number,
): boolean {
  if (spec.kind === "enum" && state.kind === "enum") {
    return matchesFacet(spec.value(row, context), state.values);
  }
  if (spec.kind === "number" && state.kind === "number") {
    return matchesRange(spec.value(row, context), state.state);
  }
  if (spec.kind === "date" && state.kind === "date") {
    const bounds = resolveDateBounds(state.state, now);
    return matchesRange(spec.value(row, context), {
      min: bounds.min,
      max: bounds.max,
      empty: state.state.empty,
    });
  }
  if (spec.kind === "text" && state.kind === "text") {
    return matchesText(spec.value(row, context), state.state);
  }
  return true;
}
