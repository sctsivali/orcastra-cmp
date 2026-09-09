import type { ScheduleFrequency, SnapshotSchedule } from "@/types/api";

/**
 * Every word and number the schedule surface uses, in one module.
 *
 * The same reasoning snapshot-origin.ts states about origins: the sort, the facet, the cell and
 * the form all read one map, so they cannot drift into two spellings of one idea on one screen.
 *
 * note: every preset label begins with a digit or a time. `check-heading-case.mjs` treats every
 * `label:` literal as a title needing Title Case and skips it only when the first character is a
 * digit, so "6 hours" is legal where "Every six hours" would fail the build. That constraint
 * happens to produce the more scannable spelling anyway.
 */

/** Mirrors the Literal on the backend's request model. check-schedule-presets.mjs holds them equal. */
export const FREQUENCIES: readonly { value: ScheduleFrequency; label: string }[] = [
  { value: "every_6_hours", label: "6 hours" },
  { value: "every_12_hours", label: "12 hours" },
  { value: "daily", label: "24 hours" },
  { value: "weekly", label: "7 days" },
];

/**
 * Mirrors the backend's keep_last bound, which is capped below the per-instance limit of 5 so an
 * operator can always still take one by hand.
 */
export const RETENTIONS: readonly { value: string; label: string }[] = [
  { value: "1", label: "1 snapshot" },
  { value: "2", label: "2 snapshots" },
  { value: "3", label: "3 snapshots" },
];

/** Quarter hours only, which is the one knob that spreads load across a fleet. */
export const MINUTES: readonly { value: string; label: string }[] = [
  { value: "0", label: "00" },
  { value: "15", label: "15" },
  { value: "30", label: "30" },
  { value: "45", label: "45" },
];

export const WEEKDAYS: readonly { value: string; label: string }[] = [
  { value: "0", label: "Monday" },
  { value: "1", label: "Tuesday" },
  { value: "2", label: "Wednesday" },
  { value: "3", label: "Thursday" },
  { value: "4", label: "Friday" },
  { value: "5", label: "Saturday" },
  { value: "6", label: "Sunday" },
];

/**
 * Hours as UTC, with the operator's own offset in the label.
 *
 * Storage is UTC because a local-time schedule at 02:30 either fires twice or not at all on a
 * daylight-saving boundary, and that arithmetic is exactly what nobody exercises. Reading it in
 * local time is a display problem, so it is solved here rather than in the column.
 */
export function hourOptions(): { value: string; label: string }[] {
  const offsetMinutes = -new Date().getTimezoneOffset();
  return Array.from({ length: 24 }, (_, hour) => {
    const utc = `${String(hour).padStart(2, "0")}:00 UTC`;
    // A viewer already on UTC gets no parenthetical, because "(00:00 UTC+00:00)" restates the
    // label it follows and, in a half-width select, pushes the useful half out of view.
    if (offsetMinutes === 0) return { value: String(hour), label: utc };
    const localMinutes = (hour * 60 + offsetMinutes + 1440) % 1440;
    const local = `${String(Math.floor(localMinutes / 60)).padStart(2, "0")}:${String(localMinutes % 60).padStart(2, "0")}`;
    return { value: String(hour), label: `${utc} \u00b7 ${local} local` };
  });
}

/**
 * The third state, derived rather than stored.
 *
 * The database holds active and paused only. A schedule that has failed is still active and will
 * try again, so inventing a stored "failing" would let a row report a state its own columns
 * contradict. The console needs the distinction to be scannable, so it derives it here, once.
 */
export type ScheduleState = "active" | "failing" | "paused" | "unknown";

export function scheduleState(schedule: SnapshotSchedule): ScheduleState {
  if (schedule.state === "paused") return "paused";
  if (schedule.state !== "active") return "unknown";
  return schedule.consecutive_failures > 0 ? "failing" : "active";
}

export const STATE_LABEL: Record<ScheduleState, string> = {
  active: "Active",
  failing: "Failing",
  paused: "Paused",
  unknown: "Unknown",
};

/** Ordered by how much attention the row wants, which is the order an operator scans for. */
export const STATE_RANK: Record<ScheduleState, number> = {
  paused: 0,
  failing: 1,
  active: 2,
  unknown: 3,
};

export const STATE_ORDER: readonly ScheduleState[] = ["paused", "failing", "active", "unknown"];

export function frequencyLabel(frequency: string): string {
  return FREQUENCIES.find((f) => f.value === frequency)?.label ?? frequency;
}

const PERIOD_HOURS: Record<ScheduleFrequency, number> = {
  every_6_hours: 6,
  every_12_hours: 12,
  daily: 24,
  weekly: 168,
};

/**
 * What this schedule will actually hold, said in one sentence under the form.
 *
 * Retention and expiry interact and the interaction is invisible: keeping 3 weekly snapshots that
 * each expire after 7 days holds exactly one, forever, and nothing on screen would say why. The
 * form does the arithmetic once here rather than leaving the operator to do it wrong every time.
 */
export function retentionFits(
  frequency: ScheduleFrequency,
  keepLast: number,
  expiryDays: number,
): boolean {
  return PERIOD_HOURS[frequency] * keepLast <= expiryDays * 24;
}

export function scheduleSentence(
  frequency: ScheduleFrequency,
  keepLast: number,
  expiryDays: number,
): string {
  const spanHours = PERIOD_HOURS[frequency] * keepLast;
  const spanDays = spanHours / 24;
  if (spanHours > expiryDays * 24) {
    return (
      `Each snapshot expires after ${expiryDays} days, which is sooner than the ` +
      `${keepLast}${ordinalSuffix(keepLast)} is taken, so this would keep fewer than ${keepLast}.`
    );
  }
  const oldest = spanDays >= 1 ? `about ${round(spanDays)} days` : `about ${spanHours} hours`;
  return `The oldest snapshot kept is ${oldest} old, and the hypervisor prunes each after ${expiryDays} days.`;
}

function round(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function ordinalSuffix(n: number): string {
  if (n === 1) return "st";
  if (n === 2) return "nd";
  if (n === 3) return "rd";
  return "th";
}
