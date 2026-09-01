// The force-stop option's copy, shared by the three confirm dialogs that offer it: the
// console toolbar, the instances list, and the console page. The strings lived inline in all
// three and stayed identical, but the code around them drifted into three variants and one
// of those shipped a footer button that never said "Force". Same reasoning as EMPTY_VALUE in
// components/ui/table-text.tsx: one definition, so the next edit cannot land in two of three
// places. The markup stays per site because the console page renders dark chrome in both
// themes and its classes are not the other two's.
//
// note: this module holds strings only. lib/ must not import from components/, per the
// layering note on EMPTY_VALUE in lib/utils.ts.

export const FORCE_OPTION_LABEL = "Force (power-cycle, don't wait for the guest)";

export const FORCE_ON_HINT =
  "Cuts power immediately, like pulling the plug. Unsaved data in the guest may be lost.";

// important: pr7-05-graceful-timeout.spec.ts matches /no OS installed|boot menu|ACPI/i
// against this text to prove the dialog explains when force is needed. Keep at least one.
export const FORCE_OFF_HINT =
  "Use this if nothing in the guest will answer a graceful shutdown: no OS installed yet, " +
  "sitting at a boot menu, still booting, or a live/installer image that ignores ACPI.";
