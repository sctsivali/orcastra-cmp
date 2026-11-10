/**
 * One utilisation ramp, for the bar and for the number beside it.
 *
 * Two token families, chosen by contrast. A bar is a non-text graphic against a 3:1 floor,
 * so its fill can use `--success` and `--destructive`, which are tuned as backgrounds. As
 * text those fail AA in light mode (3.15:1 and 3.82:1 against 4.5:1), so the percentage
 * label uses the `--status-*` set, 4.52:1 to 10.01:1 on both surfaces in both themes.
 *
 * Three bands. Half-used capacity is the working state on a hypervisor, so 50 to 74
 * percent stays green; a caution colour there teaches an operator to ignore the colour.
 */

export interface UsageTone {
  /** Class for the percentage text. AA as text in both themes. */
  text: string;
  /** Class for a bar fill, held to the 3:1 graphic floor. */
  fill: string;
  /**
   * Class for a gauge arc, an SVG stroke over `--card`.
   *
   * Uses `--status-*`: `--success` measures 3.19:1 on `--card` in light mode and 3.34:1 in
   * dark, too close to the 3:1 graphic floor for a thin arc. The status set on `--card`,
   * light then dark: online 4.95 and 7.68, pending 4.87 and 8.74, offline 6.15 and 4.84.
   */
  stroke: string;
}

export const USAGE_CRITICAL_PERCENT = 90;
export const USAGE_ELEVATED_PERCENT = 75;

/**
 * A reading as a percentage of its total, clamped to the range a bar can draw.
 *
 * A total of zero answers 0 rather than NaN or Infinity. Thin provisioning can put used
 * above total, which clamps to 100; the used-of-total text beside the bar shows the figures.
 */
export function usagePercent(used: number, total: number): number {
  if (!Number.isFinite(used) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

export type UsageLevel = "Normal" | "High" | "Critical";

/** The ramp's band in words, so the state never rests on colour alone. */
export function usageLevel(percent: number): UsageLevel {
  if (percent >= USAGE_CRITICAL_PERCENT) return "Critical";
  if (percent >= USAGE_ELEVATED_PERCENT) return "High";
  return "Normal";
}

export function usageTone(percent: number): UsageTone {
  if (percent >= USAGE_CRITICAL_PERCENT) {
    return { text: "text-status-offline", fill: "bg-destructive", stroke: "stroke-status-offline" };
  }
  if (percent >= USAGE_ELEVATED_PERCENT) {
    return { text: "text-status-pending", fill: "bg-status-pending", stroke: "stroke-status-pending" };
  }
  return { text: "text-status-online", fill: "bg-success", stroke: "stroke-status-online" };
}
