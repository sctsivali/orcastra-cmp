import { InfoToggletip } from "@/components/ui/info-toggletip";
import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import {
  CPU_ELEVATED_RATIO,
  CPU_OVERCOMMIT_WARN_RATIO,
  cpuAllocationFigure,
  cpuAllocationText,
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

const OVER = "over-committed";

/**
 * "25 vCPUs / 12 CPU threads" with the unit "CPU threads" kept whole, so a narrow card never
 * leaves "CPU" on one line and "threads" on the next. Only the unit: holding all of
 * "112 CPU threads" on one line overflows a 95 px card. CSS rather than non-breaking spaces, so
 * the text stays plain for copy, search and anything that matches it.
 */
export function CpuAllocationFigure({
  cpu,
  className,
}: {
  cpu: Pick<CpuAllocationView, "allocated" | "threads">;
  className?: string;
}) {
  return (
    <span className={className}>
      {formatVcpuCount(cpu.allocated)} / {cpu.threads}{" "}
      <span className="whitespace-nowrap">{cpu.threads === 1 ? "CPU thread" : "CPU threads"}</span>
    </span>
  );
}

/**
 * cpuAllocationText with "over-committed" kept whole, so a narrow card never breaks it at the
 * hyphen.
 */
export function CpuAllocationText({ cpu, className }: { cpu: CpuAllocationView; className?: string }) {
  const text = cpuAllocationText(cpu);
  const at = text.indexOf(OVER);
  if (at < 0) return <span className={className}>{text}</span>;
  return (
    <span className={className}>
      {text.slice(0, at)}
      <span className="whitespace-nowrap">{OVER}</span>
      {text.slice(at + OVER.length)}
    </span>
  );
}

/**
 * The allocation meter on every vCPU surface: the share of the over-commit ceiling, in the
 * band lib/cpu-allocation.ts gives it. Nothing to draw without a thread count.
 */
export function CpuAllocationMeter({ cpu, className }: { cpu: CpuAllocationView; className?: string }) {
  if (cpu.threads <= 0) return null;
  return (
    <UsageBar
      percent={cpu.meterPercent}
      level={cpu.level}
      label="vCPU allocation"
      valueText={`${cpuAllocationFigure(cpu)}, ${cpuAllocationText(cpu)}, ${Math.round(cpu.meterPercent)}% of the ${CPU_OVERCOMMIT_WARN_RATIO}x ceiling`}
      className={className}
    />
  );
}

/**
 * The ratio line under the meter, on the shared caption rule. Past the ceiling the words
 * already say "heavily over-committed", so the band is not repeated after them.
 */
export function CpuAllocationCaption({
  cpu,
  rest = "muted",
  className,
}: {
  cpu: CpuAllocationView;
  rest?: "muted" | "foreground";
  className?: string;
}) {
  return (
    <UsageCaption level={cpu.level} rest={rest} showLevel={cpu.level !== "Critical"} className={className}>
      <CpuAllocationText cpu={cpu} />
    </UsageCaption>
  );
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
        share the same threads and slow each other down.
      </p>
      <p>
        The bar fills at {CPU_OVERCOMMIT_WARN_RATIO}x, the usual ceiling for general workloads. It
        turns amber from {CPU_ELEVATED_RATIO}x and red above {CPU_OVERCOMMIT_WARN_RATIO}x.
      </p>
    </InfoToggletip>
  );
}
