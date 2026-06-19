import { useCallback, useState } from "react";
import { compareValues, type SortDirection } from "@/lib/sort";

type Accessor<T> = (item: T) => unknown;

// Generic single-column table sort. Each table supplies an accessor map keyed by
// its sort field; the accessor returns the comparable value for a column (number,
// string, array for count, or a normalized timestamp for dates). Sorting type
// awareness lives in compareValues, so tables only describe what to compare.
//
// Keep the accessors object stable (module const or useMemo([])) so sortItems
// keeps a stable identity and downstream memos do not thrash.
export function useTableSort<F extends string, T>(
  accessors: Record<F, Accessor<T>>,
  initialField: NoInfer<F>,
  initialDirection: SortDirection = "asc",
  onSortChange?: () => void,
) {
  const [sortField, setSortField] = useState<F>(initialField);
  const [sortDirection, setSortDirection] = useState<SortDirection>(initialDirection);

  const handleSort = useCallback(
    (field: F) => {
      setSortField((prev) => {
        if (prev === field) {
          setSortDirection((d) => (d === "asc" ? "desc" : "asc"));
          return prev;
        }
        setSortDirection("asc");
        return field;
      });
      onSortChange?.();
    },
    [onSortChange],
  );

  const sortItems = useCallback(
    <U extends T>(items: U[]): U[] => {
      const accessor = accessors[sortField];
      if (!accessor) return items;
      return [...items].sort((a, b) => compareValues(accessor(a), accessor(b), sortDirection));
    },
    [accessors, sortField, sortDirection],
  );

  return { sortField, sortDirection, handleSort, sortItems };
}
