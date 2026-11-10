"use client";

import * as React from "react";

import { cn, plural, singular } from "@/lib/utils";
import { SearchInput } from "@/components/ui/search-input";
import { FacetedFilter } from "@/components/ui/faceted-filter";
import { RangeFilter } from "@/components/ui/range-filter";
import { DateFilter } from "@/components/ui/date-filter";
import { TextFilter } from "@/components/ui/text-filter";
import { AddFilterMenu } from "@/components/ui/add-filter-menu";
import { ActiveFilterChips } from "@/components/ui/active-filter-chips";
import { formatBound, type TableToolbarState } from "@/hooks/use-table-filters";
import type { ResolvedFilter } from "@/lib/table-filter";

export interface TableToolbarProps<F extends string> {
  filters: TableToolbarState<F>;
  /** Plural noun for the counter sentence, ex; "profiles". */
  noun: string;
  actions?: React.ReactNode;
  className?: string;
  /**
   * The rows have not been read (still loading, or the read failed with nothing held), so there
   * is no count to state; "0 sessions" above a loader would claim an empty list.
   */
  countUnknown?: boolean;
  /**
   * The page size of the pager under this list. When every row that matches fits one page, the
   * pager is absent by convention, so the sentence says so: "Showing all 20 users" rather than
   * a bare "20 users" an operator reads as the first page of more.
   */
  pageSize?: number;
}

/** The counter sentence: total, filtered, and whether one page holds them all. */
export function countSentence({
  noun, totalCount, filteredCount, filtered, pageSize,
}: {
  noun: string;
  totalCount: number;
  filteredCount: number;
  filtered: boolean;
  pageSize?: number;
}): string {
  const total = plural(totalCount, singular(noun), noun);
  const onePage = pageSize !== undefined && filteredCount > 0 && filteredCount <= pageSize;
  // "Showing all 1 user" says nothing a bare "1 user" does not.
  if (!filtered) return onePage && totalCount > 1 ? `Showing all ${total}` : total;
  return onePage ? `Showing ${filteredCount} of ${total}` : `${filteredCount} of ${total}`;
}

/**
 * Search, filters and the result count for a list view.
 *
 * The toolbar owns the "N of M" sentence for every page. DataTablePagination renders only when
 * there is more than one page, so a count kept there would vanish once a filter fits one page.
 */
export function TableToolbar<F extends string>({
  filters,
  noun,
  actions,
  className,
  countUnknown = false,
  pageSize,
}: TableToolbarProps<F>) {
  const {
    searchInput,
    setSearchInput,
    searchPlaceholder,
    visibleFacets,
    addableFacets,
    chips,
    totalCount,
    filteredCount,
    activeFilterCount,
    setFilterState,
    toggleFacetValue,
    setFacet,
    revealFacet,
    removeChip,
    clearAll,
  } = filters;

  const chipsById = React.useMemo(() => new Map(chips.map((c) => [c.id, c])), [chips]);
  const summaryFor = (field: F) => chips.find((c) => c.field === field && c.kind !== "enum")?.valueLabel;

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <SearchInput
          value={searchInput}
          onChange={setSearchInput}
          placeholder={searchPlaceholder}
          className="sm:w-64 lg:w-80"
        />
        <div className="flex flex-wrap items-center gap-2">
          {visibleFacets.map((facet) => {
            if (facet.kind === "enum") {
              return (
                <FacetedFilter
                  key={facet.field}
                  label={facet.label}
                  options={facet.options}
                  selected={facet.selected}
                  searchThreshold={facet.searchThreshold}
                  onToggle={(value) => toggleFacetValue(facet.field, value)}
                  onClear={() => setFacet(facet.field, [])}
                />
              );
            }
            const spec = facet.spec as ResolvedFilter<unknown, unknown>;
            if (facet.kind === "number" && facet.state.kind === "number" && spec.kind === "number") {
              return (
                <RangeFilter
                  key={facet.field}
                  label={facet.label}
                  summary={summaryFor(facet.field)}
                  value={facet.state.state}
                  onChange={(next) => setFilterState(facet.field, { kind: "number", state: next })}
                  unit={spec.options.unit}
                  integer={spec.options.integer}
                  presets={spec.options.presets}
                  emptyLabel={spec.options.emptyLabel}
                  facts={facet.facts?.kind === "number" ? facet.facts : undefined}
                  formatBound={formatBound}
                />
              );
            }
            if (facet.kind === "date" && facet.state.kind === "date" && spec.kind === "date") {
              return (
                <DateFilter
                  key={facet.field}
                  label={facet.label}
                  summary={summaryFor(facet.field)}
                  value={facet.state.state}
                  onChange={(next) => setFilterState(facet.field, { kind: "date", state: next })}
                  presets={spec.options.presets}
                  emptyLabel={spec.options.emptyLabel}
                  facts={facet.facts?.kind === "date" ? facet.facts : undefined}
                />
              );
            }
            if (facet.kind === "text" && facet.state.kind === "text" && spec.kind === "text") {
              return (
                <TextFilter
                  key={facet.field}
                  label={facet.label}
                  summary={summaryFor(facet.field)}
                  value={facet.state.state}
                  onChange={(next) => setFilterState(facet.field, { kind: "text", state: next })}
                  placeholder={spec.options.placeholder}
                  emptyLabel={spec.options.emptyLabel}
                  facts={facet.facts?.kind === "text" ? facet.facts : undefined}
                />
              );
            }
            return null;
          })}
          <AddFilterMenu options={addableFacets} onAdd={(field) => revealFacet(field as F)} />
        </div>
        {actions && <div className="flex items-center gap-2 sm:ml-auto">{actions}</div>}
      </div>

      <ActiveFilterChips
        chips={chips}
        onRemove={(id) => {
          const chip = chipsById.get(id);
          if (chip) removeChip(chip);
        }}
        onClearAll={clearAll}
      />

      {/* aria-atomic announces the whole sentence. The count updates only after the search
          debounce settles, so it speaks once per settled query. "N of M" shows whenever a
          search or filter is active, even one that every row matches. */}
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        aria-busy={countUnknown}
        // min-h keeps the line's height while it is empty, so the table does not jump when the
        // count arrives.
        className="min-h-5 text-sm text-muted-foreground"
      >
        {countUnknown
          ? null
          : countSentence({ noun, totalCount, filteredCount, filtered: activeFilterCount > 0, pageSize })}
      </p>
    </div>
  );
}
