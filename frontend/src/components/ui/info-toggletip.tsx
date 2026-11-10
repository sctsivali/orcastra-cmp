"use client";

import * as React from "react";
import { Info } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

// Long enough to cross the gap from the icon to the panel, short enough not to linger.
const HOVER_CLOSE_MS = 150;

interface InfoToggletipProps {
  /** The button's accessible name, ex; "About vCPU allocation". */
  label: string;
  children: React.ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
  className?: string;
  contentClassName?: string;
}

/**
 * An (i) button that explains the value beside it: a toggletip, which opens on demand.
 *
 * important: a Radix Tooltip never opens on a tap, so an explanation behind one cannot be read on
 * a phone or tablet. This opens on click, tap, Enter and Space, and on mouse hover as a preview;
 * a click while previewing keeps it open. Esc or a click outside closes it and focus returns to
 * the button. The panel portals, so a card with overflow-hidden cannot clip it.
 */
export function InfoToggletip({
  label,
  children,
  side = "top",
  align = "center",
  className,
  contentClassName,
}: InfoToggletipProps) {
  const [open, setOpen] = React.useState(false);
  // True while the panel is only a hover preview: it closes when the pointer leaves, and it does
  // not take focus away from wherever the keyboard was.
  const previewing = React.useRef(false);
  const closeTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimer.current = setTimeout(() => {
      if (previewing.current) {
        previewing.current = false;
        setOpen(false);
      }
    }, HOVER_CLOSE_MS);
  };
  React.useEffect(() => cancelClose, []);

  const onPointerEnter = (event: React.PointerEvent) => {
    if (event.pointerType !== "mouse") return;
    cancelClose();
    if (!open) {
      previewing.current = true;
      setOpen(true);
    }
  };
  const onPointerLeave = (event: React.PointerEvent) => {
    if (event.pointerType === "mouse") scheduleClose();
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (!next) previewing.current = false;
        setOpen(next);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          onPointerEnter={onPointerEnter}
          onPointerLeave={onPointerLeave}
          onClick={(event) => {
            // Inside a clickable row or card the button explains; it does not also open the row.
            event.stopPropagation();
            if (previewing.current) {
              // The hover preview is already up: a click pins it rather than toggling it shut.
              // preventDefault stops Radix's own toggle, which runs after this handler.
              event.preventDefault();
              previewing.current = false;
              cancelClose();
            }
          }}
          className={cn(
            // The ::after box widens the hit area to 26px around a 14px icon without moving layout.
            "relative inline-flex shrink-0 items-center rounded-sm text-muted-foreground transition-colors",
            "after:absolute after:-inset-1.5 after:content-['']",
            "hover:text-foreground data-[state=open]:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
            className
          )}
        >
          <Info className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side={side}
        align={align}
        onPointerEnter={onPointerEnter}
        onPointerLeave={onPointerLeave}
        // A preview leaves focus where it was; a click, tap or key moves it into the panel so a
        // screen reader reads the explanation.
        onOpenAutoFocus={(event) => {
          if (previewing.current) event.preventDefault();
        }}
        className={cn("w-auto max-w-xs space-y-1.5 p-3 text-xs leading-relaxed", contentClassName)}
      >
        {children}
      </PopoverContent>
    </Popover>
  );
}
