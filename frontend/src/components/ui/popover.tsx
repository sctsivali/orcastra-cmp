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
      // Ensure popover stays within viewport
      collisionPadding={16}
      avoidCollisions={true}
      className={cn(
        // orcastra-popover-content is a marker, not a style. Radix gives this element
        // role="dialog", and globals.css forces position:fixed on that role so real modals stay
        // viewport-anchored. A popover must NOT be fixed: that takes it out of its popper
        // wrapper's flow, the wrapper measures 0x0, and floating-ui then positions every popover
        // as though its content had no height. The marker is how that rule excludes us.
        "orcastra-popover-content",
        // z-[100002] to appear above dialogs (z-[100001])
        "z-[100002] w-72 min-w-[var(--radix-popover-trigger-width)] rounded-md border bg-popover p-4 text-popover-foreground shadow-md outline-none",
        // Max dimensions. The Radix variable is the space actually available on the side that was
        // chosen, so the content can never be taller than the room it was given; 50vh is the
        // fallback for a Popover rendered outside a popper context. The two max-h utilities this
        // replaces were relying on CSS source order to decide a winner, which Tailwind does not
        // guarantee.
        "max-w-[calc(100vw-2rem)] max-h-[var(--radix-popper-available-height,50vh)] overflow-y-auto",
        // Prevent scroll issues
        "overscroll-contain",
        // Animations
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
