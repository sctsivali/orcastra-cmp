import type { SnapshotInfo } from "@/types/api";

/**
 * Who made a snapshot. Three answers, where the console used to have two.
 *
 * The old split was `ops-` against everything else, so a snapshot an operator took by hand over
 * SSH was labelled "Managed elsewhere" and had its actions disabled alongside the ones the
 * upstream product genuinely owns. Nothing upstream owns those and no tenant is billed for them:
 * they are the operator's own, taken outside this console, and the feature doc names three of
 * them on this very fleet as the problem the console exists to solve.
 *
 * The vocabulary lives here so the sort, the facet and the cell all read one map and cannot
 * drift. That property was claimed by the two-way helper this replaces and never tested; it is
 * tested now.
 */
export type SnapshotOrigin = "operator" | "manual" | "upstream" | "unknown";

/**
 * The row's own classification, degrading to `unknown` rather than guessing.
 *
 * important: `unknown` is a refusal, not a default. The backend decides the class and the
 * console gates a destructive action on it, so a row that arrived without one must not be
 * treated as the most permissive case. A listing that does not carry `origin` disables its
 * actions and says why.
 */
export function snapshotOrigin(snapshot: SnapshotInfo): SnapshotOrigin {
  switch (snapshot.origin) {
    case "operator":
    case "manual":
    case "upstream":
      return snapshot.origin;
    default:
      return "unknown";
  }
}

/**
 * note: "Operator" keeps its exact spelling. It is the word operators already read on this
 * column and the literal a QA spec selects rows by.
 *
 * The middle class is not called "Host": `host` is already the field name, the URL key and the
 * column label for the CLUSTER dimension on this same table, and one word meaning two things on
 * one screen is the defect the design language calls "one spelling per idea". It is not
 * "Unowned" either, because the whole correction is that an operator does own it.
 */
export const ORIGIN_LABEL: Record<SnapshotOrigin, string> = {
  operator: "Operator",
  manual: "Manual",
  upstream: "Upstream",
  unknown: "Unknown",
};

/** Ordered by how much of it this console owns, which is the order an operator scans for. */
export const ORIGIN_RANK: Record<SnapshotOrigin, number> = {
  operator: 0,
  manual: 1,
  upstream: 2,
  unknown: 3,
};

export const ORIGIN_ORDER: readonly SnapshotOrigin[] = [
  "operator",
  "manual",
  "upstream",
  "unknown",
];

/**
 * What the console must be told before it will act on this row.
 *
 * `billingLinked` is a fact about the SOURCE INSTANCE (it carries an upstream schedule) and the
 * origin is a fact about the SNAPSHOT. They are independent: an `ops-` snapshot on a scheduled
 * instance needs the acknowledgement, and an upstream snapshot on an unscheduled one needs it
 * too, for a different reason. Both can be true at once, which is why the copy is selected rather
 * than concatenated.
 */
export function snapshotGates(snapshot: SnapshotInfo, billingLinked: boolean) {
  const origin = snapshotOrigin(snapshot);
  return {
    origin,
    needsAcknowledgement: billingLinked || origin === "upstream",
    /** Delete only. Restore already makes the operator type the instance name. */
    needsTypedName: origin === "manual",
  };
}

/** Selected from three literals, never composed, so no sentence is assembled by accident. */
export function acknowledgementLabel(origin: SnapshotOrigin, billingLinked: boolean): string {
  if (origin === "upstream" && billingLinked) {
    return "I accept that this snapshot belongs to the upstream product and that acting on it moves the storage this instance is billed on";
  }
  if (origin === "upstream") {
    return "I accept that this snapshot belongs to the upstream product";
  }
  return "I accept the effect on the schedule this instance is billed under";
}
