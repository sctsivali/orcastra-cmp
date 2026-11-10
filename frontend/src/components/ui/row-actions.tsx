"use client";

import * as React from "react";
import { Loader2, MoreVertical } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export interface RowAction {
  /** Stable within one menu. Used as the React key and by specs to address an item. */
  id: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  onSelect: () => void;
  /** Paints the item with --destructive. Reserved for an action that removes something. */
  destructive?: boolean;
  /** Why this action is unavailable right now. Present means unavailable; shown under the label. */
  unavailableReason?: string;
  /** Renders a separator above this item. */
  separatorBefore?: boolean;
  /**
   * The action is running for this row. Swaps the icon for a spinner and marks the item
   * unavailable; pair it with an unavailableReason that says what is in progress.
   */
  pending?: boolean;
}

interface RowActionsProps {
  actions: RowAction[];
  /**
   * Names the trigger for a screen reader, ex; `Actions for image ubuntu/jammy`. Required: an
   * icon-only button with no name announces only as "button" (check-icon-button-names.mjs).
   *
   * Named triggerLabel rather than label: an accessible name is sentence case while a menu
   * item's `label` is Title Case, and check-heading-case.mjs checks casing by key.
   */
  triggerLabel: string;
  /**
   * Replaces the kebab glyph (the Instances table uses a gear). A glyph other than the kebab
   * gets a visible "Actions" tooltip, because a gear alone reads as "settings" rather than "the
   * menu for this row".
   */
  triggerIcon?: React.ComponentType<{ className?: string }>;
  className?: string;
}

/**
 * The kebab menu for a table row.
 *
 * important: the menu is controlled and closes before the action runs, and every item calls
 * preventDefault in onSelect, so the click cannot fall through to a clickable row. The action
 * dispatches in onCloseAutoFocus, after focus returns to the trigger, so the closing focus scope
 * cannot send a dialog's return focus to a menu item that is about to unmount.
 *
 * important: an unavailable item uses aria-disabled, never Radix's `disabled`, which drops the
 * item from keyboard focus and hover so its reason could never show (as in create-menu.tsx).
 */
export function RowActions({ actions, triggerLabel, triggerIcon, className }: RowActionsProps) {
  const [open, setOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const selectionRef = React.useRef<{
    id: string;
    trigger: HTMLButtonElement;
    triggerLabel: string;
  } | null>(null);
  const latestRef = React.useRef({ actions, triggerLabel });
  latestRef.current = { actions, triggerLabel };

  React.useEffect(() => () => { selectionRef.current = null; }, []);

  if (actions.length === 0) return null;

  const TriggerIcon = triggerIcon ?? MoreVertical;
  const trigger = (
    <DropdownMenuTrigger asChild>
      <Button
        ref={triggerRef}
        variant="ghost"
        size="sm"
        aria-label={triggerLabel}
        className={cn("h-8 w-8 p-0", className)}
        onClick={(e) => e.stopPropagation()}
      >
        <TriggerIcon className="h-4 w-4" aria-hidden="true" />
      </Button>
    </DropdownMenuTrigger>
  );

  return (
    <DropdownMenu open={open} onOpenChange={(nextOpen) => {
      if (nextOpen) selectionRef.current = null;
      setOpen(nextOpen);
    }}>
      {triggerIcon ? (
        <Tooltip>
          <TooltipTrigger asChild>
            {trigger}
          </TooltipTrigger>
          <TooltipContent side="left">Actions</TooltipContent>
        </Tooltip>
      ) : trigger}
      {/* The shared DropdownMenuContent animates with no reduced-motion guard. The override
          is scoped here, as in create-menu.tsx, rather than changing every menu in the app. */}
      <DropdownMenuContent
        align="end"
        className="w-60 motion-reduce:animate-none"
        onCloseAutoFocus={(event) => {
          const selection = selectionRef.current;
          selectionRef.current = null;
          if (!selection) return;
          event.preventDefault();
          const latest = latestRef.current;
          const action = latest.actions.find((item) => item.id === selection.id);
          if (!selection.trigger.isConnected || selection.trigger !== triggerRef.current
            || selection.triggerLabel !== latest.triggerLabel) return;
          triggerRef.current?.focus({ preventScroll: true });
          // Permissions or pending state can change during the menu's exit animation.
          if (!action || action.unavailableReason || action.pending) return;
          action.onSelect();
        }}
      >
        {actions.map((action) => {
          const Icon = action.pending ? Loader2 : action.icon;
          const unavailable = !!action.unavailableReason || !!action.pending;
          return (
            <React.Fragment key={action.id}>
              {action.separatorBefore && <DropdownMenuSeparator />}
              <DropdownMenuItem
                aria-disabled={unavailable || undefined}
                data-action={action.id}
                className={cn(
                  "items-start gap-2",
                  unavailable && "opacity-60",
                  !unavailable && action.destructive && "text-destructive focus:text-destructive",
                )}
                onSelect={(e) => {
                  e.preventDefault();
                  const trigger = triggerRef.current;
                  if (unavailable || !trigger || selectionRef.current) return;
                  selectionRef.current = { id: action.id, trigger, triggerLabel };
                  setOpen(false);
                }}
              >
                <Icon
                  className={cn(
                    "mt-0.5 h-4 w-4 shrink-0",
                    action.pending && "animate-spin motion-reduce:animate-none",
                  )}
                  aria-hidden="true"
                />
                <span className="min-w-0">
                  <span className="block">{action.label}</span>
                  {action.unavailableReason && (
                    <span className="mt-0.5 block text-xs text-muted-foreground">
                      {action.unavailableReason}
                    </span>
                  )}
                </span>
              </DropdownMenuItem>
            </React.Fragment>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
