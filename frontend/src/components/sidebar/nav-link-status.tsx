"use client";

import { Loader2 } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useLinkStatus } from "next/link";
import { cn } from "@/lib/utils";
import { useDelayedFlag } from "@/hooks/use-delayed-flag";

// important: useLinkStatus reads a context that <Link> provides around its own children,
// so both of these must render INSIDE the <Link>, never as a sibling of it.
//
// animate-spin carries no reduced-motion guard: a frozen spinner reads as a hung request,
// and a loading indicator is an essential animation under WCAG 2.3.3 (see globals.css).

// Trailing spinner for a labelled row. ml-auto pins it to the row's right edge so the
// label does not shift when it appears.
export function NavItemSpinner() {
  const { pending } = useLinkStatus();
  const show = useDelayedFlag(pending);
  if (!show) return null;
  return <Loader2 aria-hidden="true" className="ml-auto h-3.5 w-3.5 shrink-0 animate-spin" />;
}

// Icon-only rail button: swap the glyph rather than add one, so a 40px hit target keeps
// its size while the route loads.
export function NavRailIcon({ icon: Icon, className }: { icon: LucideIcon; className?: string }) {
  const { pending } = useLinkStatus();
  const show = useDelayedFlag(pending);
  const classes = cn("h-5 w-5", className);
  if (show) return <Loader2 aria-hidden="true" className={cn(classes, "animate-spin")} />;
  return <Icon className={classes} />;
}
