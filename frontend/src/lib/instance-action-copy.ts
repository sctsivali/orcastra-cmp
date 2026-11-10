// The force-stop option's copy, shared by the three confirm dialogs that offer it: the
// console toolbar, the instances list, and the console page. One definition, so an edit
// cannot land in two of three places. The markup stays per site because the console page
// renders dark chrome in both themes.
//
// Strings only, with no import from components/ (see EMPTY_VALUE in lib/utils.ts).

export const POWER_ACTION_PAST = { start: "started", stop: "stopped", restart: "restarted" } as const;

export const FORCE_OPTION_LABEL = "Force (power-cycle, don't wait for the guest)";

export const FORCE_ON_HINT =
  "Cuts power immediately, like pulling the plug. Unsaved data in the guest may be lost.";

// important: browser tests match /no OS installed|boot menu|ACPI/i against this text to
// prove the dialog explains when force is needed. Keep at least one.
export const FORCE_OFF_HINT =
  "Use this if nothing in the guest will answer a graceful shutdown: no OS installed yet, " +
  "sitting at a boot menu, still booting, or a live/installer image that ignores ACPI.";
