import { AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";

interface UnreachableNodesBannerProps {
  /** Nodes the backend could not reach while building this list. */
  nodes: string[] | undefined;
  /** What is missing, lowercase plural, ex; "networks", "storage pools". */
  noun: string;
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
  className,
}: UnreachableNodesBannerProps) {
  if (!nodes?.length) return null;

  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm",
        className,
      )}
      role="status"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
      <div>
        <span className="font-medium">
          Could not reach {nodes.length} node{nodes.length === 1 ? "" : "s"}:
        </span>{" "}
        {nodes.join(", ")}. Their {noun} are not shown, so this list may be incomplete.
      </div>
    </div>
  );
}
