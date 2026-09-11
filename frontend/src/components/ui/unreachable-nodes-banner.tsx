import { AlertTriangle } from "lucide-react";
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

/**
 * Says which nodes are missing from a list, so an outage cannot read as an empty result.
 *
 * A fan-out endpoint returns 200 with whatever it could collect. Without this, a cluster
 * that did not answer contributes nothing and looks exactly like a cluster that has
 * nothing, which is how an operator concludes a host has no images when it is simply
 * down. The list is still shown: partial data is useful as long as it is labelled.
 */
export function UnreachableNodesBanner({
  nodes,
  noun,
  reasons,
  partial = false,
  className,
}: UnreachableNodesBannerProps) {
  if (!nodes?.length) return null;

  // important: the leading "Could not reach N node(s)" clause is matched by regex in four
  // specs, so it stays byte-identical in both shapes. Only what follows it moves.
  const detailed = nodes.some((node) => reasons?.[node]);
  const lead = partial ? "Could not fully read" : "Could not reach";
  const tail = partial
    ? `Some of their ${noun} are not shown, so this list may be incomplete.`
    : `Their ${noun} are not shown, so this list may be incomplete.`;

  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm",
        className,
      )}
      role="status"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
      <div className="space-y-1">
        <div>
          <span className="font-medium">
            {lead} {nodes.length} node{nodes.length === 1 ? "" : "s"}
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
      </div>
    </div>
  );
}
