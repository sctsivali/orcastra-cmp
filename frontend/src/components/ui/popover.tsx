"use client"

import * as React from "react"
import * as PopoverPrimitive from "@radix-ui/react-popover"

import { cn } from "@/lib/utils"

const Popover = PopoverPrimitive.Root

const PopoverTrigger = PopoverPrimitive.Trigger

const PopoverContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(({ className, align = "center", sideOffset = 4, ...props }, ref) => (
  <PopoverPrimitive.Portal>
    <PopoverPrimitive.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      collisionPadding={16}
      avoidCollisions={true}
      className={cn(
        // orcastra-popover-content is a marker class with no styles. Radix gives this element
        // role="dialog", and globals.css forces position:fixed on that role so modals stay
        // viewport-anchored. A fixed popover leaves its popper wrapper's flow, the wrapper
        // measures 0x0, and floating-ui positions the popover as if it had no height. The
        // globals.css rule skips elements that carry the marker.
        "orcastra-popover-content",
        // z-[100002] sits above dialogs (z-[100001]).
        "z-[100002] w-72 min-w-[var(--radix-popover-trigger-width)] rounded-md border bg-popover p-4 text-popover-foreground shadow-md outline-none",
        // The Radix variable is the space available on the chosen side, so the content is never
        // taller than its room; 50vh is the fallback outside a popper context. One max-h utility
        // only: with two, CSS source order picks the winner, which Tailwind does not guarantee.
        "max-w-[calc(100vw-2rem)] max-h-[var(--radix-popper-available-height,50vh)] overflow-y-auto",
        // Scrolling inside the popover does not chain to the page.
        "overscroll-contain",
        "data-[state=open]:animate-in data-[state=closed]:animate-out",
        "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
        "data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
        "data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2",
        "data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2",
        className
      )}
      {...props}
    />
  </PopoverPrimitive.Portal>
))
PopoverContent.displayName = PopoverPrimitive.Content.displayName

export { Popover, PopoverTrigger, PopoverContent }
