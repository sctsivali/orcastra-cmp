"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { useDebouncedValue } from "./use-debounced-value";
import { replaceQuery } from "@/lib/url-state";
import {
  MAX_RESTING_FACETS,
  buildFacetOptions,
  matchesFilter,
  optionLabel,
  parseFacet,
  rowMatchesSearch,
  serialiseFacet,
  type CompiledTable,
  type FacetOption,
  type FilterKind,
  type FilterState,
  type ResolvedFilter,
} from "@/lib/table-filter";
import {
  DATE_PRESET_LABELS,
  INACTIVE_DATE,
  INACTIVE_RANGE,
  INACTIVE_TEXT,
  dayStart,
  isDateActive,
  isRangeActive,
  isTextActive,
  parseDate,
  parseRange,
  parseText,
  serialiseDate,
  serialiseRange,
  serialiseText,
  toDayString,
  type DateState,
  type RangeState,
} from "@/lib/filter-url";
import { formatBytes } from "@/lib/utils";

/** What a control needs to know about the rows it filters. Co-filtered, never over the raw set. */
export type FilterFacts =
  | { kind: "enum"; options: FacetOption[] }
  | { kind: "number"; min: number | null; max: number | null; emptyCount: number; matched: number; scoped: number }
  | { kind: "date"; min: number | null; max: number | null; emptyCount: number; matched: number; scoped: number }
  | { kind: "text"; emptyCount: number; matched: number; scoped: number };

export interface RenderedFacet<F extends string> {
  field: F;
  label: string;
  kind: FilterKind;
  spec: ResolvedFilter<unknown, unknown>;
  state: FilterState;
  facts: FilterFacts | undefined;
  /** A node or project dimension: always rendered, exempt from the two-value threshold. */
  pinned?: boolean;
  options: FacetOption[];
  selected: string[];
  searchThreshold?: number;
}

export interface FilterChip<F extends string> {
  id: string;
  field: F;
  kind: FilterKind;
  /** Enum only: the single value this chip removes. */
  value?: string;
  columnLabel: string;
  valueLabel: string;
}

export interface TableToolbarState<F extends string> {
  searchInput: string;
  setSearchInput: (value: string) => void;
  searchPlaceholder: string;
  visibleFacets: RenderedFacet<F>[];
  addableFacets: { field: F; label: string; kind: FilterKind }[];
  chips: FilterChip<F>[];
  totalCount: number;
  filteredCount: number;
  setFilterState: (field: F, next: FilterState) => void;
  toggleFacetValue: (field: F, value: string) => void;
  setFacet: (field: F, values: readonly string[]) => void;
  revealFacet: (field: F) => void;
  removeChip: (chip: FilterChip<F>) => void;
  clearAll: () => void;
}

export interface TableFilterOptions<F extends string, C, T = unknown> {
  context?: C;
  onFilterChange?: () => void;
  /**
   * Extra search reach for data that hangs off the row rather than sitting on it, ex; the
   * instance names inside a project. Declared search accessors are compiled at module scope and
   * cannot close over component state, which is what an index like that lives in.
   *
   * important: this ORs into the search pass, it does not bypass the filters. Overview used to
   * union its index hits back on top of the filtered rows, which rendered projects the active
   * Node facet had excluded and left the toolbar counting a different set than the grid showed.
   * Keep it referentially stable, ex; useCallback.
   */
  extraSearch?: (row: T, query: string) => boolean;
  /**
   * Facets allowed to render right now. Use for a conditional column. A pinned role does NOT
   * override this: zero options means zero rows, and a control that can pick nothing is worse
   * than no control.
   */
  availableFields?: readonly F[];
  searchDebounceMs?: number;
  /** False for a table inside a dialog, whose keys should not outlive it in the address bar. */
  syncUrl?: boolean;
}

const INACTIVE: Record<FilterKind, FilterState> = {
  enum: { kind: "enum", values: [] },
  number: { kind: "number", state: INACTIVE_RANGE },
  date: { kind: "date", state: INACTIVE_DATE },
  text: { kind: "text", state: INACTIVE_TEXT },
};

function isActiveState(state: FilterState | undefined): boolean {
  if (!state) return false;
  switch (state.kind) {
    case "enum":
      return state.values.length > 0;
    case "number":
      return isRangeActive(state.state);
    case "date":
      return isDateActive(state.state);
    case "text":
      return isTextActive(state.state);
  }
}

function parseState(spec: ResolvedFilter<unknown, unknown>, raw: string | null): FilterState {
  switch (spec.kind) {
    case "enum":
      return { kind: "enum", values: parseFacet(raw) };
    case "number":
      return { kind: "number", state: parseRange(raw, spec.options.integer) };
    case "date":
      return { kind: "date", state: parseDate(raw, spec.options.presets) };
    case "text":
      return { kind: "text", state: parseText(raw) };
  }
}

function serialiseState(state: FilterState): string | null {
  switch (state.kind) {
    case "enum":
      return state.values.length ? serialiseFacet(state.values) : null;
    case "number":
      return serialiseRange(state.state);
    case "date":
      return serialiseDate(state.state);
    case "text":
      return serialiseText(state.state);
  }
}

/** Formats a bound for display only. The stored value is always the base unit. */
export function formatBound(value: number, unit: string): string {
  if (unit === "bytes") return formatBytes(value);
  if (unit === "percent") return `${Math.round(value * 10) / 10}%`;
  if (unit === "seconds") {
    if (value < 60) return `${Math.round(value)}s`;
    if (value < 3600) return `${Math.round(value / 60)}m`;
    return `${(value / 3600).toFixed(1)}h`;
  }
  if (unit === "bps") return `${formatBytes(value / 8)}/s`;
  if (unit === "days") return `${value}d`;
  return String(value);
}

function rangeLabel(s: RangeState, unit: string, emptyLabel: string): string {
  if (s.empty === "only") return emptyLabel;
  const suffix = s.empty === "include" ? ` or ${emptyLabel}` : "";
  if (s.min !== null && s.max !== null) {
    return s.min === s.max
      ? `${formatBound(s.min, unit)}${suffix}`
      : `${formatBound(s.min, unit)} to ${formatBound(s.max, unit)}${suffix}`;
  }
  if (s.min !== null) return `at least ${formatBound(s.min, unit)}${suffix}`;
  if (s.max !== null) return `at most ${formatBound(s.max, unit)}${suffix}`;
  return emptyLabel;
}

function dateLabel(s: DateState, emptyLabel: string): string {
  if (s.preset) return DATE_PRESET_LABELS[s.preset];
  if (s.empty === "only") return emptyLabel;
  const suffix = s.empty === "include" ? ` or ${emptyLabel}` : "";
  if (s.min !== null && s.max !== null) return `${toDayString(s.min)} to ${toDayString(s.max)}${suffix}`;
  if (s.min !== null) return `after ${toDayString(s.min)}${suffix}`;
  if (s.max !== null) return `before ${toDayString(s.max)}${suffix}`;
  return emptyLabel;
}

/**
 * Client-side filtering for a table, backed by the URL.
 *
 * The companion to useTableSort: same shape, same onFilterChange page-reset contract. Order of
 * operations on a page is filter, then sortItems, then slice for the page.
 */
export function useTableFilters<T, F extends string, C = void>(
  table: CompiledTable<T, F, C>,
  rows: readonly T[],
  options: TableFilterOptions<F, C, T> = {},
) {
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const {
    context,
    onFilterChange,
    extraSearch,
    availableFields,
    searchDebounceMs = 250,
    syncUrl = true,
  } = options;

  const namespace = table.id ? `${table.id}.` : "";
  const keyFor = useCallback((field: string) => `${namespace}${field}`, [namespace]);
  const searchKey = `${namespace}q`;
  const showKey = `${namespace}show`;

  // A table inside a dialog keeps its state local: leaving its keys in the address bar after
  // the dialog closes would filter something the operator can no longer see.
  const [localParams, setLocalParams] = useState<Record<string, string>>({});
  const readParam = useCallback(
    (key: string) => (syncUrl ? searchParams.get(key) : (localParams[key] ?? null)),
    [syncUrl, searchParams, localParams],
  );

  // note: one reference for the whole render, so a date preset resolves consistently rather
  // than drifting between the predicate and the chip that describes it.
  const now = useMemo(() => dayStart(Date.now()), []);

  const urlSearch = readParam(searchKey) ?? "";

  const states = useMemo(() => {
    const result = {} as Record<F, FilterState>;
    for (const field of table.facetFields) {
      const spec = table.filterSpecs[field] as ResolvedFilter<unknown, unknown>;
      result[field] = parseState(spec, readParam(keyFor(field)));
    }
    return result;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readParam, table, keyFor]);

  const revealed = useMemo(() => new Set(parseFacet(readParam(showKey))), [readParam, showKey]);

  // Search needs to render at keystroke speed, so it is the one value with a local copy.
  const [searchInput, setSearchInput] = useState(urlSearch);
  const debouncedSearch = useDebouncedValue(searchInput, searchDebounceMs);
  const lastWrittenSearch = useRef(urlSearch);

  /**
   * The params the last write produced, which is not always what `useSearchParams` reports yet.
   *
   * important: `useSearchParams` updates on the next transition commit, so two writes landing
   * inside one commit both start from the same rendered `searchParams`, and the second silently
   * drops whatever the first added. Typing into the search box and picking a scope in the same second is enough: the
   * debounced write sets `q`, the select writes `host` from a snapshot taken before `q` existed,
   * and the typed term is gone from a URL the operator is about to share or reload. Reproduced on
   * /inventory/snapshots, two runs in three.
   *
   * Writes compose onto this ref instead, and the effect below re-seeds it whenever the URL moves
   * for any other reason: Back, Forward, a deep link, or another component's navigation.
   */
  const pendingParams = useRef<string | null>(null);

  useEffect(() => {
    pendingParams.current = searchParams.toString();
  }, [searchParams]);

  const writeParams = useCallback(
    (mutate: (params: URLSearchParams) => void) => {
      if (!syncUrl) {
        const params = new URLSearchParams(localParams);
        mutate(params);
        setLocalParams(Object.fromEntries(params.entries()));
        return;
      }
      const params = new URLSearchParams(pendingParams.current ?? searchParams.toString());
      mutate(params);
      const query = params.toString();
      pendingParams.current = query;
      // important: replace, never push. push would leave a history entry behind every checkbox
      // and every debounced keystroke, so Back would walk backwards through the filter session
      // instead of leaving the page. And a history write, not a router navigation: a navigation
      // can scroll the page to the top under the open popover (lib/url-state.ts says how).
      replaceQuery(pathname, query);
    },
    [pathname, searchParams, syncUrl, localParams],
  );

  useEffect(() => {
    if (debouncedSearch === lastWrittenSearch.current) return;
    lastWrittenSearch.current = debouncedSearch;
    writeParams((params) => {
      if (debouncedSearch) params.set(searchKey, debouncedSearch);
      else params.delete(searchKey);
    });
    onFilterChange?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch]);

  useEffect(() => {
    if (urlSearch === lastWrittenSearch.current) return;
    lastWrittenSearch.current = urlSearch;
    setSearchInput(urlSearch);
  }, [urlSearch]);

  const activeSearch = debouncedSearch;

  const searchMatches = useMemo(() => {
    const needle = activeSearch.trim().toLowerCase();
    return rows.map(
      (row) =>
        rowMatchesSearch(table, row, activeSearch) ||
        (needle.length > 0 && Boolean(extraSearch?.(row, needle))),
    );
  }, [rows, table, activeSearch, extraSearch]);

  const activeFields = useMemo(
    () => table.facetFields.filter((field) => isActiveState(states[field])),
    [table.facetFields, states],
  );

  // important: one pass-mask per ACTIVE field, computed once, rather than re-filtering the
  // whole list per field. With every column filterable a page can carry ten facets, and the
  // instances page recomputes on every LXD stream event.
  const masks = useMemo(() => {
    const result = new Map<F, Uint8Array>();
    for (const field of activeFields) {
      const spec = table.filterSpecs[field] as ResolvedFilter<T, C>;
      const state = states[field];
      const mask = new Uint8Array(rows.length);
      for (let i = 0; i < rows.length; i++) {
        mask[i] = matchesFilter(spec, state, rows[i], context as C, now) ? 1 : 0;
      }
      result.set(field, mask);
    }
    return result;
  }, [activeFields, table, states, rows, context, now]);

  const passesExcept = useCallback(
    (index: number, except: F | null) => {
      if (!searchMatches[index]) return false;
      for (const field of activeFields) {
        if (field === except) continue;
        if (!masks.get(field)?.[index]) return false;
      }
      return true;
    },
    [searchMatches, activeFields, masks],
  );

  const filteredRows = useMemo(
    () => rows.filter((_, index) => passesExcept(index, null)),
    [rows, passesExcept],
  );

  // important: facts are co-filtered, computed over the rows passing the search and every
  // OTHER active filter but not this one. Counted over the raw set, an option would promise
  // rows that vanish the moment a second filter is on, which reads as a bug.
  const facts = useMemo(() => {
    const result = {} as Record<F, FilterFacts>;
    for (const field of table.facetFields) {
      const spec = table.filterSpecs[field] as ResolvedFilter<T, C>;
      const scopedRows: T[] = [];
      for (let i = 0; i < rows.length; i++) {
        if (passesExcept(i, field)) scopedRows.push(rows[i]);
      }

      if (spec.kind === "enum") {
        result[field] = {
          kind: "enum",
          options: buildFacetOptions(table.byField[field], scopedRows, context as C),
        };
        continue;
      }

      if (spec.kind === "text") {
        let emptyCount = 0;
        for (const row of scopedRows) {
          const v = spec.value(row, context as C);
          const parts = (Array.isArray(v) ? v : [v]).filter(
            (x) => typeof x === "string" && x !== "",
          );
          if (parts.length === 0) emptyCount += 1;
        }
        result[field] = {
          kind: "text",
          emptyCount,
          scoped: scopedRows.length,
          matched: filteredRows.length,
        };
        continue;
      }

      let min: number | null = null;
      let max: number | null = null;
      let emptyCount = 0;
      for (const row of scopedRows) {
        const v = spec.value(row, context as C);
        if (v === null || v === undefined || Number.isNaN(v)) {
          emptyCount += 1;
          continue;
        }
        min = min === null || v < min ? v : min;
        max = max === null || v > max ? v : max;
      }
      result[field] = {
        kind: spec.kind,
        min,
        max,
        emptyCount,
        scoped: scopedRows.length,
        matched: filteredRows.length,
      };
    }
    return result;
  }, [table, rows, passesExcept, context, filteredRows.length]);

  const isAvailable = useCallback(
    (field: F) => !availableFields || availableFields.includes(field),
    [availableFields],
  );

  /** Does this column have anything to choose between? One option filters nothing. */
  const optionCount = useCallback(
    (field: F) => {
      const f = facts[field];
      if (!f) return 0;
      if (f.kind === "enum") return f.options.length;
      if (f.kind === "text") return f.scoped >= 1 ? 2 : 0;
      return (f.min !== null && f.max !== null && f.max > f.min) || f.emptyCount > 0 ? 2 : 0;
    },
    [facts],
  );

  const setFilterState = useCallback(
    (field: F, next: FilterState) => {
      writeParams((params) => {
        const wire = serialiseState(next);
        if (wire) params.set(keyFor(field), wire);
        else params.delete(keyFor(field));
      });
      onFilterChange?.();
    },
    [writeParams, keyFor, onFilterChange],
  );

  const setFacet = useCallback(
    (field: F, values: readonly string[]) =>
      setFilterState(field, { kind: "enum", values: [...values] }),
    [setFilterState],
  );

  const toggleFacetValue = useCallback(
    (field: F, value: string) => {
      const state = states[field];
      const current = state?.kind === "enum" ? state.values : [];
      setFacet(
        field,
        current.includes(value) ? current.filter((v) => v !== value) : [...current, value],
      );
    },
    [states, setFacet],
  );

  const revealFacet = useCallback(
    (field: F) => {
      writeParams((params) => params.set(showKey, serialiseFacet([...revealed, field])));
    },
    [writeParams, showKey, revealed],
  );

  const clearAll = useCallback(() => {
    setSearchInput("");
    lastWrittenSearch.current = "";
    writeParams((params) => {
      params.delete(searchKey);
      params.delete(showKey);
      for (const field of table.facetFields) params.delete(keyFor(field));
    });
    onFilterChange?.();
  }, [writeParams, searchKey, showKey, table.facetFields, keyFor, onFilterChange]);

  // What renders at rest, in priority order:
  //   1. a pinned role (node, project), wherever the column exists and is available
  //   2. any filter already in use, whatever its option count
  //   3. primary or revealed facets, capped at MAX_RESTING_FACETS
  //
  // important: a pinned role is exempt from the two-value threshold on purpose. One option
  // filters nothing, which is why the threshold exists for everything else, but node and
  // project are the dimensions this product is organised around and an operator should not
  // have to learn that their position moves with today's fleet.
  //
  // note: an active filter keeps its CONTROL, not just its chip. The chip makes a deep link
  // removable; the control is what makes it adjustable.
  const visibleFacets: RenderedFacet<F>[] = useMemo(() => {
    const pinned = (field: F) => table.pinnedFields.includes(field);
    const active = (field: F) => isActiveState(states[field]);

    const eligible = table.facetFields.filter((field) => {
      if (!isAvailable(field)) return false;
      if (pinned(field) || active(field)) return true;
      if (optionCount(field) < 2) return false;
      return table.primaryFacetFields.includes(field) || revealed.has(field);
    });

    // The cap counts only what the page shows on its own. Overflow falls behind "Add filter" in
    // declaration order, so the page author decides who is cut rather than the data.
    //
    // important: a revealed facet is exempt, exactly like an active filter. Budgeting it made
    // "Add filter" inert on every table that already rendered two primaries (the menu kept
    // offering the column, because addableFacets only excludes what is visible, so each further
    // click was dead too), and on the remaining tables a reveal silently evicted a control that
    // was already there instead of adding one. Revealing is a deliberate act, not a default.
    let budget = MAX_RESTING_FACETS;
    const rendered = eligible.filter((field) => {
      if (pinned(field) || active(field) || revealed.has(field)) return true;
      if (budget <= 0) return false;
      budget -= 1;
      return true;
    });

    return rendered.map((field) => {
      const spec = table.filterSpecs[field] as ResolvedFilter<unknown, unknown>;
      const f = facts[field];
      const state = states[field] ?? INACTIVE[spec.kind];
      return {
        field,
        label: table.byField[field].label,
        kind: spec.kind,
        spec,
        state,
        facts: f,
        pinned: pinned(field),
        options: f?.kind === "enum" ? f.options : [],
        selected: state.kind === "enum" ? state.values : [],
        searchThreshold: spec.kind === "enum" ? spec.spec.searchThreshold : undefined,
      };
    });
  }, [table, isAvailable, facts, revealed, states, optionCount]);

  const addableFacets = useMemo(
    () =>
      table.facetFields
        .filter(
          (field) =>
            isAvailable(field) &&
            optionCount(field) >= 2 &&
            !table.pinnedFields.includes(field) &&
            !visibleFacets.some((facet) => facet.field === field),
        )
        .map((field) => ({
          field,
          label: table.byField[field].label,
          kind: (table.filterSpecs[field] as ResolvedFilter<unknown, unknown>).kind,
        })),
    [table, isAvailable, optionCount, visibleFacets],
  );

  // important: a chip renders for every active filter even when its control is not on the
  // toolbar. That is what stops a deep link filtering the view with no way to see or undo it,
  // which is the defect the warnings page shipped with.
  const chips: FilterChip<F>[] = useMemo(
    () =>
      activeFields.flatMap((field): FilterChip<F>[] => {
        const spec = table.filterSpecs[field] as ResolvedFilter<unknown, unknown>;
        const state = states[field];
        const columnLabel = table.byField[field].label;

        if (state.kind === "enum" && spec.kind === "enum") {
          return state.values.map((value) => ({
            id: `${field}:${value}`,
            field,
            kind: "enum" as const,
            value,
            columnLabel,
            valueLabel: optionLabel(value, spec.spec),
          }));
        }

        const emptyLabel = (spec.kind !== "enum" ? spec.options.emptyLabel : undefined) ?? "(none)";
        let valueLabel = "";
        if (state.kind === "number") {
          valueLabel = rangeLabel(
            state.state,
            spec.kind === "number" ? spec.options.unit : "count",
            emptyLabel,
          );
        } else if (state.kind === "date") {
          valueLabel = dateLabel(state.state, emptyLabel);
        } else if (state.kind === "text") {
          valueLabel =
            state.state.empty === "only"
              ? emptyLabel
              : `contains "${state.state.query}"`;
        }
        return [{ id: `${field}:*`, field, kind: spec.kind, columnLabel, valueLabel }];
      }),
    [activeFields, states, table],
  );

  const removeChip = useCallback(
    (chip: FilterChip<F>) => {
      if (chip.kind === "enum" && chip.value !== undefined) {
        toggleFacetValue(chip.field, chip.value);
        return;
      }
      const spec = table.filterSpecs[chip.field] as ResolvedFilter<unknown, unknown>;
      setFilterState(chip.field, INACTIVE[spec.kind]);
    },
    [toggleFacetValue, setFilterState, table],
  );

  /** Selected enum values for one field, for a page rendering its own control for it. */
  const selectedValues = useCallback(
    (field: F) => {
      const state = states[field];
      return state?.kind === "enum" ? state.values : [];
    },
    [states],
  );

  return {
    states,
    selectedValues,
    searchInput,
    setSearchInput,
    searchPlaceholder: table.searchPlaceholder,
    filteredRows,
    totalCount: rows.length,
    filteredCount: filteredRows.length,
    visibleFacets,
    addableFacets,
    chips,
    activeFilterCount: chips.length + (activeSearch ? 1 : 0),
    setFilterState,
    setFacet,
    toggleFacetValue,
    revealFacet,
    removeChip,
    clearAll,
  };
}
