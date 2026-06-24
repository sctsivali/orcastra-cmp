import { useEffect, useState } from "react";

// Debounce a fast-changing value (ex; a search input) so downstream effects /
// queries only fire after the user pauses typing. No external dependency.
export function useDebouncedValue<T>(value: T, delayMs = 250): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
