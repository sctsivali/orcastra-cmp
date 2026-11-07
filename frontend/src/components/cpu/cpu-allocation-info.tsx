import { InfoToggletip } from "@/components/ui/info-toggletip";
import {
  CPU_OVERCOMMIT_WARN_RATIO,
  formatCpuThreads,
  type CpuAllocationView,
} from "@/lib/cpu-allocation";
import { formatVcpuCount } from "@/lib/utils";

interface CpuAllocationInfoProps {
  /** The figures this explains. Without them the explanation is general. */
  cpu?: Pick<CpuAllocationView, "allocated" | "threads" | "ratioLabel">;
  /** What the figure covers, as the sentence names it: "cluster", "cluster member", "fleet". */
  scope?: string;
  className?: string;
}

/**
 * The (i) beside every "vCPU Allocation" heading: what vCPUs and CPU threads are, and why a
 * ratio above 1.0x is a normal state rather than a fault.
 */
export function CpuAllocationInfo({ cpu, scope = "cluster", className }: CpuAllocationInfoProps) {
  const known = cpu && cpu.threads > 0;
  return (
    <InfoToggletip label="About vCPU allocation" className={className}>
      {known ? (
        <p>
          Running instances on this {scope} are given {formatVcpuCount(cpu.allocated)} in total. Its
          hosts have {formatCpuThreads(cpu.threads)}, so the allocation is {cpu.ratioLabel}.
        </p>
      ) : (
        <p>Running instances are given vCPUs; the hosts supply CPU threads.</p>
      )}
      <p>
        A CPU thread is one processor as Linux counts it: cores &times; threads per core. A vCPU is
        what an instance sees as one processor.
      </p>
      <p>
        Above 1.0x the {scope} is over-committed: normal for light workloads, but busy instances
        share the same threads and slow each other down. Above {CPU_OVERCOMMIT_WARN_RATIO}x is
        flagged.
      </p>
    </InfoToggletip>
  );
}
