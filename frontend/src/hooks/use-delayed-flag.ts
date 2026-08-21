"use client";

import * as React from "react";

// Suppresses sub-threshold flashes: returns true only once `value` has stayed true for
// `delayMs`. A navigation that commits in 80ms therefore shows no indicator at all, which
// is what keeps a fast hop from looking busier than a slow one.
export function useDelayedFlag(value: boolean, delayMs = 150): boolean {
  const [delayed, setDelayed] = React.useState(false);

  React.useEffect(() => {
    if (!value) {
      setDelayed(false);
      return;
    }
    const timer = setTimeout(() => setDelayed(true), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return delayed;
}
