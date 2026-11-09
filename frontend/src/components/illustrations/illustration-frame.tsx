import * as React from "react";
import { cn } from "@/lib/utils";
import { CANVAS } from "./iso/projection";

/**
 * fit follows the width of the card holding it (`illo-kpi` or `illo-card`, illustrations.css); the
 * others are fixed 4:3 boxes.
 */
export type IllustrationSize = "fit" | "md" | "lg" | "xl";

// Fixed boxes at the canvas's 4:3 ratio, so the card reserves the same room before and after
// hydration and a scene never shifts the text beside it. fit is sized by its card's container query,
// which the stylesheet also resolves before first paint.
const SIZE: Record<IllustrationSize, string> = {
  fit: "",
  md: "h-[126px] w-[168px]",
  lg: "h-36 w-48",
  xl: "h-[174px] w-[232px]",
};

/**
 * A crop of the canvas, [x, y, width, height] in canvas units. Every scene passes one measured from
 * its own drawn bounds: padded 4 units, widened to 4:3, bottom aligned, and never narrower than 104
 * units, so a single object fills its frame like a wide composition without zooming past ~1.5x and
 * all scenes keep one apparent scale. Re-measure after moving geometry.
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
 * Wraps what a surface showed before it had a scene; illustrations.css shows it only when scenes
 * are hidden. A span, so it is valid inside a button or a heading as well as a block.
 */
export function IllustrationFallback({ children }: { children: React.ReactNode }) {
  return <span data-slot="illustration-fallback">{children}</span>;
}
