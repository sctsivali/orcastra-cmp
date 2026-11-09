"use client";

import { ChevronRight, type LucideIcon } from "lucide-react";

import { Card } from "@/components/ui/card";
import { IllustrationFallback } from "@/components/illustrations/illustration-frame";
import { CpuIllustration, GpuIllustration, MemoryIllustration, StorageIllustration } from "@/components/illustrations/scenes/hardware";
import { cn } from "@/lib/utils";

export interface TechnologySummaryItem {
  label: string;
  value: string;
  detail?: string;
}

export type TechnologyKind = "cpu" | "memory" | "storage" | "gpu";

// The hardware each card is about. The icon comes back in the scene's place when illustrations are
// hidden (components/illustrations/illustrations.css).
const ILLUSTRATIONS = {
  cpu: CpuIllustration,
  memory: MemoryIllustration,
  storage: StorageIllustration,
  gpu: GpuIllustration,
} as const;

interface TechnologySummaryCardProps {
  kind: TechnologyKind;
  icon: LucideIcon;
  title: string;
  description: string;
  items: readonly TechnologySummaryItem[];
  onOpen: () => void;
  className?: string;
}

/** A real, keyboard-reachable dialog opener that retains the visual language of a summary card. */
export function TechnologySummaryCard({
  kind,
  icon: Icon,
  title,
  description,
  items,
  onOpen,
  className,
}: TechnologySummaryCardProps) {
  const Illustration = ILLUSTRATIONS[kind];
  return (
    <Card className={cn("overflow-hidden", className)}>
      <button
        type="button"
        aria-haspopup="dialog"
        aria-label={`View ${title} details`}
        onClick={onOpen}
        className={cn(
          "illo-host group block w-full rounded-xl text-left",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
        )}
      >
        <span className="block p-6 pb-4">
          <span className="flex items-start gap-3">
            <IllustrationFallback>
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-foreground text-background">
                <Icon className="h-5 w-5" aria-hidden="true" />
              </span>
            </IllustrationFallback>
            <span className="min-w-0 flex-1">
              <span className="block text-lg font-semibold leading-tight">{title}</span>
              <span className="mt-1 block text-sm text-muted-foreground">{description}</span>
            </span>
            <Illustration size="md" className="-my-3 -mr-2" />
          </span>
        </span>

        <span className="block px-6 pb-5">
          {items.map((item) => (
            <span
              key={item.label}
              className="mb-4 block border-b pb-4 last:mb-0 last:border-b-0 last:pb-0"
            >
              <span className="flex items-start justify-between gap-4">
                <span className="shrink-0 text-sm font-medium text-muted-foreground">
                  {item.label}
                </span>
                <span className="min-w-0 break-words text-right text-sm font-semibold tabular-nums">
                  {item.value}
                </span>
              </span>
              {item.detail ? (
                <span className="mt-1 block text-xs text-muted-foreground">{item.detail}</span>
              ) : null}
            </span>
          ))}
        </span>

        <span className="flex items-center justify-between border-t px-6 py-3 text-sm font-medium">
          View details
          <ChevronRight
            className="h-4 w-4 text-muted-foreground motion-safe:transition-transform motion-safe:group-hover:translate-x-0.5"
            aria-hidden="true"
          />
        </span>
      </button>
    </Card>
  );
}
