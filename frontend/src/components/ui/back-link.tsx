"use client";

import { ChevronLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { IntentLink } from "@/components/navigation/route-intent";
import { cn } from "@/lib/utils";

interface BackLinkProps {
  /** The explicit destination. */
  href: string;
  /** The destination, as a noun phrase: "Settings", "Clusters", "Extensions". */
  label: string;
  className?: string;
}

/**
 * The way out of a page that is reached by clicking into something.
 *
 * important: an explicit destination through `IntentLink`, never `router.back()`. Browser back
 * cannot be prefetched, raises no progress bar (the bar is driven by anchor clicks and by
 * `navigate()`), and lands somewhere unpredictable when the page was reached by deep link.
 * `IntentLink` prefetches on hover and on focus, so a keyboard gets the same as a pointer.
 *
 * The label collapses to "Back" below `sm`, where the header has to share a line.
 */
export function BackLink({ href, label, className }: BackLinkProps) {
  return (
    <Button asChild variant="ghost" size="sm" className={cn("shrink-0", className)}>
      <IntentLink href={href}>
        <ChevronLeft className="h-4 w-4 mr-1" />
        <span className="hidden sm:inline">Back to {label}</span>
        <span className="sm:hidden">Back</span>
      </IntentLink>
    </Button>
  );
}
