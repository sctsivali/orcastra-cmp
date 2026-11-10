import type { SnapshotInfo } from "@/types/api";

/**
 * Who made a snapshot: this console (`operator`), an operator by hand outside it (`manual`), or
 * the upstream product (`upstream`); `unknown` when the row does not say. A hand-taken snapshot
 * is the operator's own: nothing upstream owns it and no tenant is billed for it.
 *
 * The vocabulary lives here so the sort, the facet and the cell all read one map.
 */
export type SnapshotOrigin = "operator" | "manual" | "upstream" | "unknown";

/**
 * The row's own classification, degrading to `unknown` rather than guessing.
 *
 * important: treat `unknown` as a refusal. The backend decides the class and the console gates
 * destructive actions on it, so a row without one must never get the most permissive case. A
 * listing that does not carry `origin` disables its actions and says why.
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
 * "Operator" keeps its exact spelling: operators already read it on this column, and an
 * end-to-end spec selects rows by that literal.
 *
 * The middle class is not "Host", because `host` already names the CLUSTER dimension on this
 * table (field, URL key and column label), and not "Unowned", because an operator owns it.
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
 * origin is a fact about the SNAPSHOT. A billing link or an `upstream` origin each requires the
 * acknowledgement, for different reasons, and both can hold at once, so the copy is selected
 * rather than concatenated.
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
