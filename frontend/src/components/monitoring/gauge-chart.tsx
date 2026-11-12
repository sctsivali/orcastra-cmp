import { cn } from "@/lib/utils";
import { usageLevel, usagePercent, usageTone } from "@/lib/usage-tone";
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

  const level = usageLevel(percentage);
  const tone = usageTone(percentage);
  const flagged = reported && level !== "Normal";

  // The status message explains an empty or zero gauge, so the tooltip appears only then.
  const showTooltip = percentage === 0 && statusMessage;

  // The arc is a picture of the number, so the container is the meter and the SVG is hidden
  // from assistive technology rather than read out as a string of shapes.
  const gaugeContent = (
    <div
      className="flex flex-col items-center"
      // A meter needs a value, so a gauge with no reading is announced as a labelled image.
      role={reported ? "meter" : "img"}
      aria-label={
        reported
          ? `${label}: ${percentage.toFixed(1)}%${flagged ? `, ${level.toLowerCase()}` : ""}`
          : `${label}: ${emptyLabel}`
      }
      aria-valuemin={reported ? 0 : undefined}
      aria-valuemax={reported ? 100 : undefined}
      aria-valuenow={reported ? Math.round(percentage) : undefined}
      data-level={reported ? level.toLowerCase() : undefined}
    >
      <svg width={size} height={size / 2 + 20} className="transform" aria-hidden="true">
        <path
          d={arc}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-muted"
        />
        {/* Value arc, on the same usage ramp as every capacity bar (lib/usage-tone.ts), GPU
            allocation included. No arc without a reading: a round cap on a zero-length dash
            still paints a dot. */}
        {reported ? (
          <path
            d={arc}
            fill="none"
            strokeWidth={strokeWidth}
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            className={cn(
              "[transition-property:stroke-dashoffset,stroke] duration-500 motion-reduce:transition-none",
              tone.stroke,
            )}
          />
        ) : null}
        {/* Value text. The empty label is a word, not a figure, so it is smaller and muted:
            "Unavailable" at the figure's 20px bold would be wider than the gauge and clip. */}
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
      <span className="text-sm font-medium text-muted-foreground mt-1">
        {label}
        {/* The band in words beside the arc, as under every bar: colour is never the only cue. */}
        {flagged ? <span className={cn("whitespace-nowrap", tone.text)}> · {level}</span> : null}
      </span>
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
