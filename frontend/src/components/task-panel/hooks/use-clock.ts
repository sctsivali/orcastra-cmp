"use client";

import * as React from "react";

const LIVE_TICK_MS = 1_000;
const IDLE_TICK_MS = 30_000;

/** One second while a row is running and the tab is visible, else a slow tick for "5m ago". */
export function useClock(live: boolean): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    let id: number | undefined;
    const stop = () => {
      if (id !== undefined) window.clearInterval(id);
      id = undefined;
    };
    const start = () => {
      stop();
      id = window.setInterval(() => setNow(Date.now()), live ? LIVE_TICK_MS : IDLE_TICK_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        setNow(Date.now());
        start();
      } else {
        stop();
      }
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [live]);
  return now;
}

