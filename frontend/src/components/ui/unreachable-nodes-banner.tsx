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
    reasons: data.node_errors && typeof data.node_errors === "object" ? data.node_errors : undefined,
  };
}

interface UnreachableNodesBannerProps {
  /** Nodes the backend could not reach while building this list. */
  nodes: string[] | undefined;
  /**
   * Nodes that answered but lost some rows (a pool that refused, a project that could not be
   * read). They share the one banner with `nodes`: two stacked cards for one list read as two
   * problems, and neither said how many clusters were short in all.
   */
  partialNodes?: string[] | undefined;
  /** What is missing, lowercase plural, ex; "networks", "storage pools". */
  noun: string;
  /**
   * What a partly read cluster is missing, when that is less than `noun`. The Overview reads
   * projects and instances, and a cluster whose projects answered lost only its instances.
   */
  partialNoun?: string;
  /**
   * Why each node did not answer, keyed by its name in `nodes` or `partialNodes`, shown under
   * Show Details. A name with no reason still lists: a payload cached by an older build carries
   * none.
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
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

// Past this many rows the list scrolls inside the banner, so a fleet-wide outage cannot push the
// page it is warning about off the screen.
const SCROLL_AFTER = 8;

// The API prefixes a reason with its kind. Under "Could not reach" the prefix says nothing twice.
const REASON_KIND = /^(?:unreachable|forbidden|error|partial):\s*/;

/**
 * The sentence for both shapes at once. A partial read says "could not fully read", so a regex on
 * "Could not reach" cannot mistake it for an outage, and "some", because most of its rows are there.
 *
 * important: the leading "Could not reach N cluster(s)" clause is matched by regex in four specs,
 * so it stays byte-identical whenever an unreachable cluster exists. Only what follows it moves.
 */
function sentenceFor(
  unreachable: number,
  partial: number,
  noun: string,
  partialNoun: string = noun,
): { lead: string; tail: string } {
  const incomplete = "so this list may be incomplete.";
  if (unreachable > 0 && partial > 0) {
    return {
      lead: `Could not reach ${plural(unreachable, "cluster")} and could not fully read ${partial}.`,
      tail: `Some ${noun} are not shown, ${incomplete}`,
    };
  }
  if (unreachable > 0) {
    return {
      lead: `Could not reach ${plural(unreachable, "cluster")}.`,
      tail: `${unreachable === 1 ? "Its" : "Their"} ${noun} are not shown, ${incomplete}`,
    };
  }
  return {
    lead: `Could not fully read ${plural(partial, "cluster")}.`,
    tail: `Some of ${partial === 1 ? "its" : "their"} ${partialNoun} are not shown, ${incomplete}`,
  };
}

/**
 * The caution shell, written once for both shapes.
 *
 * note: a token border and a toned icon on the card, not a wash. It was a 10% amber tint under
 * amber-600, the raw palette shape scripts/checks/check-semantic-colour.mjs ratchets; the prose
 * already inherited --foreground, so only the icon and the border carry the caution.
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
 * It takes `noun` and nothing else, deliberately. A component that merely declines to render
 * `nodes` is one careless edit away from rendering it. A component that cannot be handed a node
 * name needs a type change first, and scripts/checks/check-banner-role-gate.mjs pins this
 * signature so that change cannot be a quiet one.
 *
 * No count either. How many nodes a fleet has is not something this operator is told anywhere
 * else in the console.
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
  noun,
  partialNoun,
  reasons,
  className,
  open,
  onOpenChange,
}: DetailProps) {
  const { lead, tail } = sentenceFor(unreachable.length, partial.length, noun, partialNoun);
  const partly = new Set(partial);
  const sorted = [...unreachable, ...partial].sort((a, b) => a.localeCompare(b));
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
              const detail = partly.has(node) ? (reason ? `Partly read · ${reason}` : "Partly read") : reason;
              return (
                // note: index in the key. Callers pass node names, which are unique, but a
                // duplicate string here must not silently drop a row from a warning banner.
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
 * result. Who is told WHICH node depends on the role.
 *
 * A fan-out endpoint returns 200 with whatever it could collect. Without this, a cluster that
 * did not answer contributes nothing and looks exactly like a cluster that has nothing, which is
 * how an operator concludes a host has no images when it is simply down. The list is still
 * shown: partial data is useful as long as it is labelled.
 *
 * Admin and Partner hold cluster:read and get the names, and on /technologies the per-node
 * reason as well, because for them the banner is a diagnosis. A tenant holds no cluster:read, is
 * scoped to instances, and is told the fleet's node names nowhere else in the console, so naming
 * them here would be the one surface that does. The tenant still gets the fact that the list is
 * incomplete, which is the part of this banner that is theirs: without it, an instance sitting on
 * a dead node simply vanishes from their list and reads as deleted.
 *
 * important: this is a UI redaction, not enforcement. The routers still send unreachable_nodes in
 * the payload. Backend scoping is the real boundary, and for a tenant with no policy assignment
 * that list is now empty at the source (`_narrow_by_tenant_acl` in app/core/org_scope.py).
 */
export function UnreachableNodesBanner({
  nodes,
  partialNodes,
  noun,
  partialNoun,
  reasons,
  className,
}: UnreachableNodesBannerProps) {
  // important: the hook runs before every early return, so the emptiness check cannot move above it.
  const { isLoading, hasRole } = usePermissions();
  // important: above the early returns too. A list that drops to no missing clusters for one
  // refetch and comes back must not close what the operator opened.
  const [open, setOpen] = useState(false);

  const unreachable = nodes ?? [];
  // A cluster that could not be reached cannot also have been read in part. The fan-out keeps the
  // two apart already; this keeps the list naming each cluster once if a caller ever does not.
  const reached = new Set(unreachable);
  const partial = (partialNodes ?? []).filter((node) => !reached.has(node));
  if (unreachable.length === 0 && partial.length === 0) return null;

  // important: nothing, rather than the wrong copy, while the first session read is outstanding.
  // usePermissions reports isLoading only when there is no session to read roles from, which is
  // first load alone: a background useSession().update() keeps the previous session and leaves
  // isLoading false, so a partner never watches this flip from the tenant line to the named one.
  // With no session resolveRoles([]) answers [Role.TENANT], so rendering here would show the
  // tenant line to everybody for a frame. A dead session lands in the same branch, which is the
  // right way for a disclosure gate to fail.
  if (isLoading) return null;

  // hasRole(Role.PARTNER), not can(Permission.CLUSTER_READ). The permission names the protected
  // thing more precisely and has zero other call sites in this tree, while seven sibling gates
  // read exactly like this line. scripts/checks/check-banner-role-gate.mjs asserts the two stay
  // equivalent against ROLE_PERMISSIONS, so the precision is kept without a second vocabulary.
  if (!hasRole(Role.PARTNER)) {
    return <UnreachableNodesNotice noun={noun} className={className} />;
  }

  return (
    <UnreachableNodesDetail
      unreachable={unreachable}
      partial={partial}
      noun={noun}
      partialNoun={partialNoun}
      reasons={reasons}
      className={className}
      open={open}
      onOpenChange={setOpen}
    />
  );
}
