"use client"

import * as React from "react"
import * as ProgressPrimitive from "@radix-ui/react-progress"

import { cn } from "@/lib/utils"

interface ProgressProps extends React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root> {
  /** An animated shimmer over the fill. */
  animated?: boolean;
  /** A gradient fill instead of a solid one. */
  gradient?: boolean;
}

const Progress = React.forwardRef<
  React.ElementRef<typeof ProgressPrimitive.Root>,
  ProgressProps
>(({ className, value, animated = false, gradient = false, ...props }, ref) => {
  // important: held inside [0, 100]. Radix rejects a value past max (it logs and falls back to
  // indeterminate), and the transform below would slide the fill out of its track, ex; an
  // over-committed member's CPU at 383 %.
  const clamped = Math.min(100, Math.max(0, value ?? 0));
  return (
  <ProgressPrimitive.Root
    ref={ref}
    value={clamped}
    className={cn(
      "relative h-4 w-full overflow-hidden rounded-full bg-secondary",
      className
    )}
    {...props}
  >
    <ProgressPrimitive.Indicator
      className={cn(
        "h-full w-full flex-1 transition-all duration-300 ease-out",
        gradient 
          ? "bg-gradient-to-r from-primary via-primary/80 to-primary" 
          : "bg-primary",
        animated && "relative overflow-hidden"
      )}
      style={{ transform: `translateX(-${100 - clamped}%)` }}
    >
      {animated && (
        <div 
          className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer"
          style={{ 
            backgroundSize: '200% 100%',
            animation: 'shimmer 2s ease-in-out infinite'
          }}
        />
      )}
    </ProgressPrimitive.Indicator>
  </ProgressPrimitive.Root>
  );
})
Progress.displayName = ProgressPrimitive.Root.displayName

export { Progress }
