import { InfoToggletip } from "@/components/ui/info-toggletip";
import { GPU_CAPACITY_NOTE, GPU_UTILIZATION_NOTE } from "@/lib/gpu";

/**
 * States that the console reports GPU allocation rather than utilization. A toggletip rather
 * than a tooltip, so it opens on tap and by keyboard; the accessible name carries the meaning.
 */
export function GpuUtilizationInfo({ className }: { className?: string }) {
  return (
    <InfoToggletip label="Why no GPU utilization is shown" className={className}>
      <p>{GPU_UTILIZATION_NOTE}</p>
    </InfoToggletip>
  );
}

/** The (i) beside a GPU heading: which cards count as GPUs for instances, and which do not. */
export function GpuCapacityInfo({ className }: { className?: string }) {
  return (
    <InfoToggletip label="About GPUs for instances" className={className}>
      <p>{GPU_CAPACITY_NOTE}</p>
      <p>{GPU_UTILIZATION_NOTE}</p>
    </InfoToggletip>
  );
}
