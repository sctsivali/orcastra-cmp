import { Badge } from "@/components/ui/badge";
import { GPU_CLASS_LABEL, gpuClassOf } from "@/lib/gpu";
import type { GPUCardInfo } from "@/types/api";

// Taxonomy, not status, so no state tone: filled for capacity, outline for
// attachable-but-not-capacity, muted for a card that cannot compute.
const VARIANT = {
  compute: "secondary",
  integrated: "outline",
  display: "muted",
  unknown: "muted",
} as const;

/** Always rendered: an absent badge would read the same as an unclassified card. */
export function GpuClassBadge({ card }: { card: GPUCardInfo }) {
  const cls = gpuClassOf(card);
  return <Badge variant={VARIANT[cls]}>{GPU_CLASS_LABEL[cls]}</Badge>;
}
