import type * as React from "react";

// What the dialog knows about the row it was opened from.
//
// note: this is built on click, never on render. A page renders one of these adapters per row
// only when a human asks for it, so the cost of assembling a subject never enters the table's
// render path.
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
   * important: a ReactNode, not a string. The six pages that feed this format their values with
   * three different rules: formatLabel for a real API enum, formatBytes for a size, and raw for
   * a user-authored name that must never be re-cased. A string prop would force the dialog to
   * pick one of the three, and the architecture/x86_64 precedent shows how that ends.
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
 * A discriminated union over PANELS rather than over pages.
 *
 * important: the difference matters for how this file ages. A union over subjects would carry
 * one member per inventory page, so every new page would edit this shared type and grow it.
 * These five are shared across pages instead: `config` appears on five of six, and `custom` is
 * the escape hatch so a genuine one-off never forces a seventh member.
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
