"use client";

import { ReactNode, useState } from "react";
import { AlertTriangle, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { cn, plural } from "@/lib/utils";
import type { FanoutReachability } from "@/types/api";

export interface Reachability {
  unreachable: string[];
  partial: string[];
  late: string[];
  reasons?: Record<string, string>;
}

/**
 * The reachability fields of a list payload, with the shape every page expects. A payload
 * cached by an older build carries none of them, and several hooks type their payload as a
 * plain record, so this reads them once instead of each page casting.
 */
export function reachabilityOf(payload: unknown): Reachability {
  const data = (payload ?? {}) as FanoutReachability;
  return {
    unreachable: Array.isArray(data.unreachable_nodes) ? data.unreachable_nodes : [],
    partial: Array.isArray(data.partial_nodes) ? data.partial_nodes : [],
    late: Array.isArray(data.late_nodes) ? data.late_nodes : [],
    reasons: data.node_errors && typeof data.node_errors === "object" ? data.node_errors : undefined,
  };
}

interface UnreachableNodesBannerProps {
  /** Nodes the backend could not reach while building this list. */
  nodes: string[] | undefined;
  /**
   * Nodes that answered but lost some rows (a pool that refused, a project that could not be
   * read). They share the one banner with `nodes`, so one list shows one card with one total.
   */
  partialNodes?: string[] | undefined;
  /**
   * Nodes still reading past the backend's per-cluster wait (late_nodes). Not an outage: their
   * rows arrive with a later answer, so they get their own sentence and are marked in the list.
   */
  lateNodes?: string[] | undefined;
  /** What is missing, lowercase plural, ex; "networks", "storage pools". */
  noun: string;
  /**
   * What a partly read cluster is missing, when that is less than `noun`. The Overview reads
   * projects and instances, and a cluster whose projects answered lost only its instances.
   */
  partialNoun?: string;
  /**
   * Why each node did not answer, keyed by its name in `nodes` or `partialNodes`, shown under
   * Show Details. A name with no reason is still listed.
   *
   * important: a reason can carry LXD's own error text. It travels on the same role gate as the
   * names and never reaches a role without cluster:read.
   */
  reasons?: Record<string, string>;
  className?: string;
}

type DetailProps = Pick<UnreachableNodesBannerProps, "noun" | "partialNoun" | "reasons" | "className"> & {
  unreachable: string[];
  partial: string[];
  late: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

// Past this many rows the list scrolls inside the banner, so a fleet-wide outage cannot push the
// page it is warning about off the screen.
const SCROLL_AFTER = 8;

// The API prefixes a reason with its kind. Under "Could not reach" the prefix says nothing twice.
const REASON_KIND = /^(?:unreachable|forbidden|error|partial|late):\s*/;

/**
 * The sentence for both shapes at once. A partial read says "could not fully read", so a regex on
 * "Could not reach" cannot mistake it for an outage, and "some", because most of its rows are there.
 *
 * important: the browser specs in frontend/e2e match the leading "Could not reach N cluster(s)"
 * clause by regex, so it stays byte-identical whenever an unreachable cluster exists.
 */
function sentenceFor(
  unreachable: number,
  partial: number,
  noun: string,
  partialNoun: string = noun,
  late: number = 0,
): { lead: string; tail: string } {
  const incomplete = "so this list may be incomplete.";
  // A cluster still reading is not missing for good: its rows come with a later answer.
  const stillLoading =
    late > 0 ? ` Still loading ${late} more, shown when ${late === 1 ? "it answers" : "they answer"}.` : "";
  if (unreachable === 0 && partial === 0) {
    return {
      lead: `Still loading ${plural(late, "cluster")}.`,
      tail: `${late === 1 ? "Its" : "Their"} ${noun} are shown when ${late === 1 ? "it answers" : "they answer"}.`,
    };
  }
  if (unreachable > 0 && partial > 0) {
    return {
      lead: `Could not reach ${plural(unreachable, "cluster")} and could not fully read ${partial}.`,
      tail: `Some ${noun} are not shown, ${incomplete}${stillLoading}`,
    };
  }
  if (unreachable > 0) {
    return {
      lead: `Could not reach ${plural(unreachable, "cluster")}.`,
      tail: `${unreachable === 1 ? "Its" : "Their"} ${noun} are not shown, ${incomplete}${stillLoading}`,
    };
  }
  return {
    lead: `Could not fully read ${plural(partial, "cluster")}.`,
    tail: `Some of ${partial === 1 ? "its" : "their"} ${partialNoun} are not shown, ${incomplete}${stillLoading}`,
  };
}

/**
 * The caution shell, written once for both shapes. A token border and a toned icon carry the
 * caution rather than a tinted wash (see scripts/checks/check-semantic-colour.mjs); the prose
 * stays --foreground.
 *
 * important: `role="status"` sits on the sentence alone. It is a live region, so a control or a
 * long list inside it would be read out again on every background refetch.
 */
function BannerShell({
  className,
  sentence,
  children,
}: {
  className?: string;
  sentence: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div
      data-slot="unreachable-banner"
      className={cn(
        "flex items-start gap-2 rounded-lg border border-status-pending/40 bg-card p-3 text-sm",
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-pending" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div role="status">{sentence}</div>
        {children}
      </div>
    </div>
  );
}

/**
 * What a role without cluster:read sees.
 *
 * It takes `noun` and nothing else, so handing it a node name needs a type change, and
 * scripts/checks/check-banner-role-gate.mjs pins this signature. No count either: this role is
 * not told the size of the fleet anywhere else in the console.
 */
function UnreachableNodesNotice({ noun, className }: { noun: string; className?: string }) {
  return (
    <BannerShell
      className={className}
      sentence={`Some ${noun} could not be loaded right now, so this list may be incomplete.`}
    />
  );
}

/** What Admin and Partner see: one sentence, and the clusters behind Show Details. */
function UnreachableNodesDetail({
  unreachable,
  partial,
  late,
  noun,
  partialNoun,
  reasons,
  className,
  open,
  onOpenChange,
}: DetailProps) {
  const { lead, tail } = sentenceFor(unreachable.length, partial.length, noun, partialNoun, late.length);
  const partly = new Set(partial);
  const loading = new Set(late);
  const sorted = [...unreachable, ...partial, ...late].sort((a, b) => a.localeCompare(b));
  const scrolls = sorted.length > SCROLL_AFTER;

  return (
    <BannerShell
      className={className}
      sentence={
        <>
          <span className="font-medium">{lead}</span> {tail}
        </>
      }
    >
      <Collapsible open={open} onOpenChange={onOpenChange}>
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="-ml-2 mt-1 h-8 px-2 text-muted-foreground">
            <ChevronDown
              className={cn("mr-1 h-4 w-4 transition-transform motion-reduce:transition-none", open && "rotate-180")}
              aria-hidden="true"
            />
            {open ? "Hide Details" : "Show Details"}
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <ul
            aria-label={`Clusters behind this notice, ${sorted.length} in total`}
            // A list that scrolls takes focus, or the rows past the fold are mouse-only.
            tabIndex={scrolls ? 0 : undefined}
            className={cn("space-y-0.5 pt-1", scrolls && "max-h-60 overflow-y-auto")}
          >
            {sorted.map((node, i) => {
              const reason = reasons?.[node]?.replace(REASON_KIND, "");
              // A cluster that answered is marked, so the list can be checked against the
              // sentence: the unmarked rows are the ones that could not be reached at all.
              const detail = partly.has(node)
                ? (reason ? `Partly read · ${reason}` : "Partly read")
                : loading.has(node)
                  ? (reason ? `Still loading · ${reason}` : "Still loading")
                  : reason;
              return (
                // Index in the key: callers pass unique node names, but a duplicate string here
                // must not silently drop a row from a warning banner.
                <li key={`${node}-${i}`} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium">{node}</span>
                  {detail ? <span className="min-w-0 break-words">{detail}</span> : null}
                </li>
              );
            })}
          </ul>
        </CollapsibleContent>
      </Collapsible>
    </BannerShell>
  );
}

/**
 * Says a list is short because a node did not answer, so an outage cannot read as an empty
 * result: a fan-out endpoint returns 200 with whatever it could collect. Who is told which node
 * depends on the role.
 *
 * Admin and Partner hold cluster:read and get the names (and on /technologies the per-node
 * reason). A tenant holds no cluster:read and is told node names nowhere else, so it gets only
 * the fact that the list is incomplete; without it an instance on a dead node reads as deleted.
 *
 * important: this is only a UI redaction. The routers still send unreachable_nodes; backend
 * scoping is the boundary, and for a tenant with no policy assignment that list is empty at the
 * source (`_narrow_by_tenant_acl` in app/core/org_scope.py).
 */
export function UnreachableNodesBanner({
  nodes,
  partialNodes,
  lateNodes,
  noun,
  partialNoun,
  reasons,
  className,
}: UnreachableNodesBannerProps) {
  // important: the hook runs before every early return, so the emptiness check cannot move above it.
  const { isLoading, hasRole } = usePermissions();
  // important: above the early returns too, so a list that drops to no missing clusters for one
  // refetch and comes back keeps open what the operator opened.
  const [open, setOpen] = useState(false);

  const unreachable = nodes ?? [];
  // A cluster that could not be reached cannot also have been read in part. The fan-out keeps the
  // two apart already; this keeps the list naming each cluster once if a caller ever does not.
  const reached = new Set(unreachable);
  const partial = (partialNodes ?? []).filter((node) => !reached.has(node));
  const partlySet = new Set(partial);
  const late = (lateNodes ?? []).filter((node) => !reached.has(node) && !partlySet.has(node));
  if (unreachable.length === 0 && partial.length === 0 && late.length === 0) return null;

  // important: render nothing while the first session read is outstanding. With no session
  // resolveRoles([]) answers [Role.TENANT], so rendering here would show the tenant line to
  // everybody for a frame. isLoading is true only on first load (a background
  // useSession().update() keeps the previous session); a dead session also fails closed here.
  if (isLoading) return null;

  // hasRole(Role.PARTNER) rather than can(Permission.CLUSTER_READ), matching the sibling role
  // gates. scripts/checks/check-banner-role-gate.mjs asserts the two stay equivalent against
  // ROLE_PERMISSIONS.
  if (!hasRole(Role.PARTNER)) {
    return <UnreachableNodesNotice noun={noun} className={className} />;
  }

  return (
    <UnreachableNodesDetail
      unreachable={unreachable}
      partial={partial}
      late={late}
      noun={noun}
      partialNoun={partialNoun}
      reasons={reasons}
      className={className}
      open={open}
      onOpenChange={setOpen}
    />
  );
}
