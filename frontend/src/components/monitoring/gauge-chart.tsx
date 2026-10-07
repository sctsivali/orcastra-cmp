import { usagePercent } from "@/lib/usage-tone";
import {
  Tooltip as UITooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface GaugeChartProps {
  /** null draws an empty arc with `emptyLabel`, never a zero reading. */
  value: number | null;
  max?: number;
  label: string;
  color?: string;
  size?: number;
  statusMessage?: string; // Optional status message to show on hover when value is 0
  /** What the gauge says when there is no reading, ex; "Calculating…". */
  emptyLabel?: string;
}

export function GaugeChart({
  value,
  max = 100,
  label,
  color = "#3b82f6",
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
  
  // Determine color based on percentage
  const getColor = () => {
    if (percentage >= 90) return "#ef4444"; // red
    if (percentage >= 75) return "#f59e0b"; // amber
    return color;
  };

  // Show tooltip only when value is 0 and we have a status message
  const showTooltip = percentage === 0 && statusMessage;

  const gaugeContent = (
    <div className="flex flex-col items-center">
      <svg width={size} height={size / 2 + 20} className="transform">
        {/* Background arc */}
        <path
          d={`M ${strokeWidth / 2} ${size / 2} A ${radius} ${radius} 0 0 1 ${size - strokeWidth / 2} ${size / 2}`}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-muted"
        />
        {/* Value arc */}
        <path
          d={`M ${strokeWidth / 2} ${size / 2} A ${radius} ${radius} 0 0 1 ${size - strokeWidth / 2} ${size / 2}`}
          fill="none"
          stroke={getColor()}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="transition-all duration-500"
        />
        {/* Value text */}
        <text
          x={size / 2}
          y={size / 2}
          textAnchor="middle"
          className="fill-foreground text-xl font-bold"
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
