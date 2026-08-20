import { useEffect, useState } from "react";

/**
 * Trailing-edge debounce for a rapidly changing value, ex; a search box.
 *
 * note: the caller renders the raw value and filters on the debounced one, so typing stays
 * responsive while the expensive work and the URL write happen once the user pauses.
 */
export function useDebouncedValue<T>(value: T, delayMs = 250): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    if (Object.is(value, debounced)) return;
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs, debounced]);

  return debounced;
}
