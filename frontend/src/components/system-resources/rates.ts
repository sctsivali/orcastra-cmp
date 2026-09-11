/**
 * Bytes per second between two samples, from the server's own timestamps.
 *
 * The panel used to divide a counter delta by a hard-coded five seconds, which was only right
 * while the poll was exactly five seconds apart: a manual refresh a second after a poll
 * reported a rate five times too small. Null until there are two samples, and null when the
 * window is too short to mean anything.
 */
export function ratePerSecond(
  prev: number | null | undefined,
  curr: number,
  prevAt: string | undefined,
  currAt: string,
): number | null {
  if (prev === null || prev === undefined || !prevAt) return null;
  const seconds = (Date.parse(currAt) - Date.parse(prevAt)) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0.5) return null;
  const delta = curr - prev;
  return delta > 0 ? delta / seconds : 0;
}
