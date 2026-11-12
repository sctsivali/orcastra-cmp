import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { toneForLevel, type UsageLevel } from "@/lib/usage-tone";

export interface UsageCaptionProps {
  /** The band of the reading this caption sits beside, from usageLevel or the reading's own scale. */
  level: UsageLevel;
  /** The reading, ex; "57.4% used". */
  children: ReactNode;
  /**
   * The colour while Normal: "muted" for a caption under a bar, "foreground" for a value in a
   * label row. High and Critical replace it with the ramp's text tone either way.
   */
  rest?: "muted" | "foreground";
  /**
   * Append the band in words at High and Critical. Off only where the reading's own words
   * already name the state, ex; "5.2x, heavily over-committed".
   */
  showLevel?: boolean;
  className?: string;
}

/**
 * The one rule for the number beside a capacity bar, on every page.
 *
 * Neutral while Normal. A green number on every row teaches an operator to stop reading the
 * colour, so colour is kept for the readings that need attention. From High up the text takes
 * the ramp's `--status-*` tone, which holds AA as text in both themes (lib/usage-tone.ts), and
 * the band is spelled out, so the state never rests on colour alone (WCAG 1.4.1).
 */
export function UsageCaption({
  level,
  children,
  rest = "muted",
  showLevel = true,
  className,
}: UsageCaptionProps) {
  const flagged = level !== "Normal";
  return (
    <span
      className={cn(
        "tabular-nums",
        flagged ? toneForLevel(level).text : rest === "muted" ? "text-muted-foreground" : "text-foreground",
        className,
      )}
      data-level={level.toLowerCase()}
    >
      {children}
      {flagged && showLevel ? (
        <>
          {" "}
          <span className="whitespace-nowrap font-medium">· {level}</span>
        </>
      ) : null}
    </span>
  );
}
