import type * as React from "react";

// What the dialog knows about the row it was opened from.
//
// Built on click, never on render, so assembling a subject stays out of the table's render path.
export interface UsedBySubject {
  /** The row's own kind, ex; "networks". Names the noun in the header. */
  kind: string;
  name: string;
  /** The node the used_by list came from, and the `host` for any link out of this dialog. */
  node?: string;
  /** The project that holds the row. A network detail link without it reads the default project. */
  project?: string;
  description?: string;
  usedBy: readonly string[];
  /** Dense header facts, pre-formatted by the page. The dialog lays them out, it does not format. */
  facts?: readonly SubjectFact[];
  /** Extra panels beyond Usage. No panels means no tab strip at all. */
  panels?: readonly SubjectPanel[];
}

export interface SubjectFact {
  label: string;
  /**
   * A ReactNode because pages format values with different rules (formatLabel for an API enum,
   * formatBytes for a size, raw for a user-authored name that must never be re-cased), and a
   * string prop would force the dialog to pick one.
   */
  value: React.ReactNode;
}

export interface UsageDict {
  used?: number;
  total?: number;
  [key: string]: unknown;
}

export interface AclRule {
  action?: string;
  source?: string;
  destination?: string;
  protocol?: string;
  source_port?: string;
  destination_port?: string;
  icmp_type?: string;
  icmp_code?: string;
  state?: string;
  description?: string;
}

/**
 * A discriminated union over panels rather than over pages, so a new inventory page reuses these
 * members instead of growing the type. `custom` covers a one-off.
 */
export type SubjectPanel =
  | { id: string; label: string; kind: "config"; entries?: Record<string, string> }
  | { id: string; label: string; kind: "devices"; devices?: Record<string, Record<string, string>> }
  | { id: string; label: string; kind: "aclRules"; ingress?: AclRule[]; egress?: AclRule[] }
  | { id: string; label: string; kind: "capacity"; space?: UsageDict; inodes?: UsageDict }
  | { id: string; label: string; kind: "custom"; render: () => React.ReactNode };

/** Which view the Usage panel opens in. The two table columns want different ones. */
export type UsageView = "flat" | "grouped";

export const USAGE_PANEL_ID = "usage";
