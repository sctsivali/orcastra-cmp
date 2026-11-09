import * as React from "react";
import { cn } from "@/lib/utils";
import { CANVAS } from "./iso/projection";

export type IllustrationSize = "sm" | "md" | "lg";

// Fixed boxes at the canvas's 4:3 ratio, so the card reserves the same room before and after
// hydration and a scene never shifts the text beside it.
const SIZE: Record<IllustrationSize, string> = {
  sm: "h-12 w-16",
  md: "h-24 w-32",
  lg: "h-[120px] w-40",
};

export interface IllustrationProps {
  size?: IllustrationSize;
  className?: string;
}

/**
 * The only <svg> root an illustration renders through. A scene is decoration: it repeats what the
 * card's text already says, so it is hidden from assistive technology, takes no pointer events and
 * no focus, and `html[data-illustrations="hide"]` removes it without leaving a gap
 * (illustrations.css).
 */
export function IllustrationFrame({
  size = "md",
  className,
  children,
}: IllustrationProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox={`0 0 ${CANVAS.width} ${CANVAS.height}`}
      aria-hidden="true"
      focusable="false"
      data-slot="card-illustration"
      data-size={size}
      className={cn("pointer-events-none shrink-0 select-none overflow-visible", SIZE[size], className)}
    >
      <g className="illo-scene">{children}</g>
    </svg>
  );
}

/**
 * Wraps what a surface showed before it had a scene; illustrations.css shows it only when scenes
 * are hidden. A span, so it is valid inside a button or a heading as well as a block.
 */
export function IllustrationFallback({ children }: { children: React.ReactNode }) {
  return <span data-slot="illustration-fallback">{children}</span>;
}
