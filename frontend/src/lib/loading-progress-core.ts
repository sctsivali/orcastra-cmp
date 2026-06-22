/**
 * Pure, framework-agnostic helpers shared by the simulated and shared-route
 * loading progress hooks. Keeping these here avoids duplicating the easing
 * curve and phase thresholds across hooks (and a circular import with the
 * message tables in use-loading-progress.ts).
 */

// Loading phases keyed by progress. These match the keys in DYNAMIC_MESSAGES
// (excluding the terminal "complete" phase, which is only used on finish).
export type LoadingPhaseKey =
  | "initializing"
  | "connecting"
  | "fetching"
  | "processing"
  | "rendering"
  | "almostDone";

// Cap simulated progress below 100 while loading; the final jump to 100 happens
// when the real work completes (the loader unmounts).
export const PROGRESS_CAP = 95;

// Logarithmic easing: fast at the start, slows as it approaches the cap.
export function computeSimulatedProgress(elapsedMs: number, minDuration = 800): number {
  return Math.min(PROGRESS_CAP, (1 - Math.exp(-elapsedMs / (minDuration * 2))) * 100);
}

export function getPhaseKey(progress: number): LoadingPhaseKey {
  if (progress < 15) return "initializing";
  if (progress < 30) return "connecting";
  if (progress < 50) return "fetching";
  if (progress < 70) return "processing";
  if (progress < 90) return "rendering";
  return "almostDone";
}
