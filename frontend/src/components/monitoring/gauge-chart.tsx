import { cn } from "@/lib/utils";
import { usagePercent, usageTone } from "@/lib/usage-tone";
import {
  Tooltip as UITooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface GaugeChartProps {
  /** null draws the empty track with `emptyLabel`, never a zero reading. */
  value: number | null;
  max?: number;
  label: string;
  size?: number;
  /** Shown on hover when there is no reading, ex; why the cluster did not answer. */
  statusMessage?: string;
  /** What the gauge says when there is no reading, ex; "Calculating…". */
  emptyLabel?: string;
}

export function GaugeChart({
  value,
  max = 100,
  label,
  size = 120,
  statusMessage,
  emptyLabel = "Unavailable",
}: GaugeChartProps) {
  const reported =
    value !== null && Number.isFinite(value) && value >= 0 && Number.isFinite(max) && max > 0;
  const percentage = reported ? usagePercent(value, max) : 0;
  const strokeWidth = 12;
  const radius = (size - strokeWidth) / 2;
  const circumference = radius * Math.PI; // Half circle
  const offset = circumference - (percentage / 100) * circumference;
  const arc = `M ${strokeWidth / 2} ${size / 2} A ${radius} ${radius} 0 0 1 ${size - strokeWidth / 2} ${size / 2}`;

  // Show tooltip only when value is 0 and we have a status message
  const showTooltip = percentage === 0 && statusMessage;

  const gaugeContent = (
    <div className="flex flex-col items-center">
      <svg width={size} height={size / 2 + 20} className="transform">
        {/* Background arc */}
        <path
          d={arc}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-muted"
        />
        {/* Value arc, on the same usage ramp as every usage bar (lib/usage-tone.ts). No arc
            without a reading: a round cap on a zero-length dash still paints a dot. */}
        {reported ? (
          <path
            d={arc}
            fill="none"
            strokeWidth={strokeWidth}
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            className={cn(
              "transition-all duration-500 motion-reduce:transition-none",
              usageTone(percentage).stroke,
            )}
          />
        ) : null}
        {/* Value text. The empty label is a word, not a figure, so it is smaller and muted:
            "Unavailable" at the figure's 20px bold is wider than the gauge and was clipped. */}
        <text
          x={size / 2}
          y={size / 2}
          textAnchor="middle"
          className={
            reported
              ? "fill-foreground text-xl font-bold tabular-nums"
              : "fill-muted-foreground text-sm font-medium"
          }
        >
          {reported ? `${percentage.toFixed(1)}%` : emptyLabel}
        </text>
      </svg>
      <span className="text-sm font-medium text-muted-foreground mt-1">{label}</span>
    </div>
  );

  if (showTooltip) {
    return (
      <UITooltip>
        <TooltipTrigger asChild>
          <div className="cursor-help">
            {gaugeContent}
          </div>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">
          <p className="text-sm">{statusMessage}</p>
        </TooltipContent>
      </UITooltip>
    );
  }

  return gaugeContent;
}
