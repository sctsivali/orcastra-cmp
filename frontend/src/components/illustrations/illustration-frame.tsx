import * as React from "react";
import { cn } from "@/lib/utils";
import { CANVAS } from "./iso/projection";

/** sm sits in the corner of a KPI tile or beside a data card's title; md and up hold a card's side. */
export type IllustrationSize = "sm" | "md" | "lg" | "xl";

// Fixed boxes at the canvas's 4:3 ratio, so the card reserves the same room before and after
// hydration and a scene never shifts the text beside it. A card kind may step a size down by its own
// width (illustrations.css), which the stylesheet also resolves before first paint.
const SIZE: Record<IllustrationSize, string> = {
  sm: "h-[66px] w-[88px]",
  md: "h-[126px] w-[168px]",
  lg: "h-36 w-48",
  xl: "h-[174px] w-[232px]",
};

/**
 * A crop of the canvas, [x, y, width, height] in canvas units, measured per scene from the rendered
 * silhouette. Each silhouette covers 30% of its frame, centred, on a ground line 12% above the
 * bottom, so scenes in one row share a weight and a baseline; a scene too tall or too wide for that
 * is fitted inside an 8% margin instead. Re-measure after moving geometry.
 */
export type IllustrationView = readonly [number, number, number, number];

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
  view = [0, 0, CANVAS.width, CANVAS.height],
  className,
  children,
}: IllustrationProps & { view?: IllustrationView; children: React.ReactNode }) {
  return (
    <svg
      viewBox={view.join(" ")}
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
 * Wraps what a surface shows in place of its scene; illustrations.css shows it only when scenes
 * are hidden. A span, so it is valid inside a button or a heading as well as a block.
 */
export function IllustrationFallback({ children }: { children: React.ReactNode }) {
  return <span data-slot="illustration-fallback">{children}</span>;
}
