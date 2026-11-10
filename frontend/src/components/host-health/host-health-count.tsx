"use client";

import { Badge } from "@/components/ui/badge";
import { useHostHealth } from "@/hooks/use-host-health";
import { plural } from "@/lib/utils";

/**
 * How many of the dashboard's dependencies need attention, beside the Host Health nav item.
 * Nothing renders while all are healthy, while the report loads, or when it cannot be read: a
 * failed read is not a count.
 */
export function HostHealthCount() {
  const { data } = useHostHealth({ poll: "badge" });
  if (!data || data.attention === 0) return null;
  const down = data.checks.some((check) => check.status === "down");
  // The number is the Badge's own text rather than a <span>: the sidebar's custom-colour
  // rules repaint every span to the sidebar text colour, which would land on the chip's red
  // or amber fill. The spoken form sits beside it, where its colour does not matter.
  return (
    <>
      <Badge
        aria-hidden="true"
        variant={down ? "negative" : "caution"}
        className="ml-auto h-5 min-w-5 justify-center px-1.5 tabular-nums"
      >
        {data.attention}
      </Badge>
      <span className="sr-only">
        {`, ${plural(data.attention, "service needs", "services need")} attention`}
      </span>
    </>
  );
}

/** The same number for the collapsed rail, where the group icon carries it. */
export function useHostHealthAttention(enabled: boolean): number {
  const { data } = useHostHealth({ poll: "badge" });
  return enabled && data ? data.attention : 0;
}
