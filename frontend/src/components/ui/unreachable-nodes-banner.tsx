"use client";

import { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { cn } from "@/lib/utils";
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
  /** What is missing, lowercase plural, ex; "networks", "storage pools". */
  noun: string;
  /**
   * Why each node did not answer, keyed by its name in `nodes`. A fan-out list endpoint only
   * reports which nodes it lost, so most callers have nothing to pass here and the names are
   * joined inline. Where the payload does carry a reason, listing it is the difference between
   * "a node is down" and a diagnosis the operator can act on.
   *
   * important: a reason is raw connection text, so it carries hosts, ports and TLS detail. It
   * travels on the same role gate as the names and never reaches a role without cluster:read.
   */
  reasons?: Record<string, string>;
  /**
   * The nodes answered but lost some rows (a pool that refused, a project that could not be
   * read). The lead clause changes so a regex on "Could not reach" cannot mistake a partial
   * read for an outage, and the tail says "some of", because most rows are there.
   */
  partial?: boolean;
  className?: string;
}

type DetailProps = Omit<UnreachableNodesBannerProps, "nodes"> & { nodes: string[] };

/**
 * The amber shell, written once for both shapes.
 *
 * important: this is the only bg-amber-500/10 in the file and it has to stay that way.
 * scripts/checks/check-semantic-colour.mjs counts at-rest tints whose foreground could be on
 * another line and holds the tree at a baseline of 37, which is exactly where it sits. Writing
 * the container twice, once per role branch, makes it 38 and fails the gate for a duplicated
 * string rather than for a colour decision.
 */
function BannerShell({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm",
        className,
      )}
      role="status"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
      <div className="space-y-1">{children}</div>
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
    <BannerShell className={className}>
      <div>Some {noun} could not be loaded right now, so this list may be incomplete.</div>
    </BannerShell>
  );
}

/** What Admin and Partner see. Unchanged from the shape that shipped. */
function UnreachableNodesDetail({ nodes, noun, reasons, partial = false, className }: DetailProps) {
  // important: the leading "Could not reach N node(s)" clause is matched by regex in four
  // specs, so it stays byte-identical in both shapes. Only what follows it moves.
  const detailed = nodes.some((node) => reasons?.[node]);
  const lead = partial ? "Could not fully read" : "Could not reach";
  const tail = partial
    ? `Some of their ${noun} are not shown, so this list may be incomplete.`
    : `Their ${noun} are not shown, so this list may be incomplete.`;

  return (
    <BannerShell className={className}>
      <div>
        <span className="font-medium">
          {lead} {nodes.length} cluster{nodes.length === 1 ? "" : "s"}
          {detailed ? "." : ":"}
        </span>{" "}
        {detailed ? null : <>{nodes.join(", ")}. </>}
        {tail}
      </div>
      {detailed ? (
        <ul className="space-y-0.5">
          {nodes.map((node, i) => (
            // note: index in the key. Callers pass node names, which are unique, but a
            // duplicate string here must not silently drop a row from a warning banner.
            <li key={`${node}-${i}`} className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium">{node}</span>
              {/* note: no --muted-foreground here. The amber tint is a 10% wash over the page
                  background, so muted measures the same 4.27:1 it does on --background and
                  would fail AA. The node name is already distinguished by weight. */}
              <span className="break-words">{reasons?.[node] || "no reason reported"}</span>
            </li>
          ))}
        </ul>
      ) : null}
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
  noun,
  reasons,
  partial = false,
  className,
}: UnreachableNodesBannerProps) {
  // important: the hook runs before every early return, so the emptiness check cannot move above it.
  const { isLoading, hasRole } = usePermissions();

  if (!nodes?.length) return null;

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
      nodes={nodes}
      noun={noun}
      reasons={reasons}
      partial={partial}
      className={className}
    />
  );
}
