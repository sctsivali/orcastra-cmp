/**
 * One utilisation ramp, for the bar and for the number beside it.
 *
 * Two families of token, and the split is measured rather than aesthetic. A bar is a
 * non-text graphic against a 3:1 floor, and `--success` / `--warning` / `--destructive`
 * are tuned as backgrounds that carry their own foreground, so they are right there. The
 * same tokens fail AA as text in light mode (documentation/ui/cluster-status-vocabulary.md
 * measures them at 3.15:1, 2.73:1 and 3.82:1 against a 4.5:1 floor), so the percentage
 * label uses the `--status-*` set instead, measured at 4.52:1 to 10.01:1 across both
 * surfaces in both themes.
 *
 * Three bands, not four. The member resource modal used to paint 50 to 74 percent yellow,
 * which is not a warning on a hypervisor: half-used capacity is the working state, and a
 * caution colour there teaches an operator to stop reading the colour at all.
 */

export interface UsageTone {
  /** Class for the percentage text. AA as text in both themes. */
  text: string;
  /** Class for a bar fill. Measured as a graphic, not as text. */
  fill: string;
}

export const USAGE_CRITICAL_PERCENT = 90;
export const USAGE_ELEVATED_PERCENT = 75;

export function usageTone(percent: number): UsageTone {
  if (percent >= USAGE_CRITICAL_PERCENT) {
    return { text: "text-status-offline", fill: "bg-destructive" };
  }
  if (percent >= USAGE_ELEVATED_PERCENT) {
    return { text: "text-status-pending", fill: "bg-status-pending" };
  }
  return { text: "text-status-online", fill: "bg-success" };
}
