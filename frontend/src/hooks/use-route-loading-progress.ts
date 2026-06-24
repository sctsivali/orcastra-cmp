"use client";

/**
 * Shared loading progress for full-page loaders (route transition + data fetch).
 *
 * The route-transition loader (App Router loading.tsx) and the page's own
 * data-fetch loader mount at different moments. Backing their progress with a
 * single module-level clock lets the second loader resume where the first left
 * off, so the user perceives one continuous whale instead of two with a reset.
 *
 * Module state is mutated only inside effects (client-side), so server renders
 * just read the initial 0 - SSR-safe. Scope is intentionally limited to the
 * full-page loaders; LoadingOverlay keeps its own independent progress
 * (useSimulatedProgress) to avoid cross-talk.
 */

import * as React from "react";
import { DYNAMIC_MESSAGES } from "./use-loading-progress";
import {
  computeSimulatedProgress,
  getPhaseKey,
  type LoadingPhaseKey,
} from "@/lib/loading-progress-core";

const MIN_DURATION = 800;
const MESSAGE_ROTATION_MS = 2500;
// Grace window to bridge the unmount->mount handoff between two full-page
// loaders. Longer than a transition gap, shorter than a fresh navigation pause.
const RESET_GRACE_MS = 250;

// Shared clock across every mounted full-page loader instance.
let startTime: number | null = null;
let mountCount = 0;
let resetTimer: ReturnType<typeof setTimeout> | null = null;

function messageFor(phase: LoadingPhaseKey, index: number): string {
  const messages = DYNAMIC_MESSAGES[phase];
  return messages[index % messages.length];
}

export function useSharedRouteProgress(): { progress: number; statusMessage: string } {
  const [progress, setProgress] = React.useState(0);
  const [statusMessage, setStatusMessage] = React.useState(
    () => DYNAMIC_MESSAGES.initializing[0],
  );

  React.useEffect(() => {
    mountCount += 1;
    if (resetTimer) {
      clearTimeout(resetTimer);
      resetTimer = null;
    }
    if (startTime === null) {
      startTime = Date.now();
    }

    let frame = 0;
    let messageIndex = 0;
    let lastPhase: LoadingPhaseKey = getPhaseKey(
      computeSimulatedProgress(Date.now() - startTime, MIN_DURATION),
    );
    setStatusMessage(messageFor(lastPhase, messageIndex));

    const tick = () => {
      const elapsed = Date.now() - (startTime ?? Date.now());
      const value = computeSimulatedProgress(elapsed, MIN_DURATION);
      setProgress(value);

      const phase = getPhaseKey(value);
      if (phase !== lastPhase) {
        lastPhase = phase;
        messageIndex = 0;
        setStatusMessage(messageFor(phase, messageIndex));
      }

      if (value < 95) {
        frame = requestAnimationFrame(tick);
      }
    };
    frame = requestAnimationFrame(tick);

    // Rotate messages within the current phase to keep the loader lively.
    const rotation = setInterval(() => {
      messageIndex += 1;
      setStatusMessage(messageFor(lastPhase, messageIndex));
    }, MESSAGE_ROTATION_MS);

    return () => {
      cancelAnimationFrame(frame);
      clearInterval(rotation);
      mountCount -= 1;
      if (mountCount <= 0) {
        mountCount = 0;
        if (resetTimer) clearTimeout(resetTimer);
        resetTimer = setTimeout(() => {
          startTime = null;
          resetTimer = null;
        }, RESET_GRACE_MS);
      }
    };
  }, []);

  return { progress, statusMessage };
}
