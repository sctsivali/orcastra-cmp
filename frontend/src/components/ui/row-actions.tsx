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
import { cn } from "@/lib/utils";

export interface RowAction {
  /** Stable within one menu. Used as the React key and by specs to address an item. */
  id: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  onSelect: () => void;
  /** Paints the item with --destructive. Reserved for an action that removes something. */
  destructive?: boolean;
  /**
   * Why this action is unavailable right now. Present means unavailable, and the string is
   * shown under the label rather than only being a reason the code knows about.
   */
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
   * Names the trigger for a screen reader, ex; `Actions for image ubuntu/jammy`. Required:
   * an icon-only button with no name announces as "button" and nothing else, which is the
   * defect `scripts/checks/check-icon-button-names.mjs` exists to stop regrowing.
   *
   * note: named triggerLabel rather than label because an accessible name is sentence case
   * while a menu item's `label` is Title Case. check-heading-case.mjs reads the key, so two
   * different casing contracts must not share one name.
   */
  triggerLabel: string;
  className?: string;
}

/**
 * The kebab menu for a table row. One implementation, because three details here are easy to
 * get wrong and each one has already been gotten wrong somewhere in this repo.
 *
 * important: the menu is controlled and closes itself before the handler runs, and every item
 * calls preventDefault in onSelect. A row that is itself clickable otherwise sees the click
 * fall through. Dispatch after onCloseAutoFocus so the outgoing modal focus scope cannot
 * redirect the dialog's return target to a menu item that is about to unmount.
 *
 * important: an unavailable item is marked with aria-disabled, never with Radix's `disabled`
 * prop. @radix-ui/react-menu passes `focusable: !disabled` to the roving-focus item and
 * filters disabled items out of the collections that drive arrow keys and typeahead, and the
 * house item class adds data-[disabled]:pointer-events-none on top. A `disabled` item is
 * therefore unreachable by keyboard and emits no hover, so the reason it is unavailable could
 * never be surfaced to anyone. Here the item stays focusable and explains itself instead.
 * Same reasoning as components/create-menu/create-menu.tsx.
 */
export function RowActions({ actions, triggerLabel, className }: RowActionsProps) {
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

  return (
    <DropdownMenu open={open} onOpenChange={(nextOpen) => {
      if (nextOpen) selectionRef.current = null;
      setOpen(nextOpen);
    }}>
      <DropdownMenuTrigger asChild>
        <Button
          ref={triggerRef}
          variant="ghost"
          size="sm"
          aria-label={triggerLabel}
          className={cn("h-8 w-8 p-0", className)}
          onClick={(e) => e.stopPropagation()}
        >
          <MoreVertical className="h-4 w-4" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      {/* note: the shared DropdownMenuContent animates in and out with no reduced-motion
          guard. Fixing the primitive would touch every menu in the app, so the override is
          scoped here, as create-menu.tsx already does. */}
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
          // note: permissions or pending state can change during the menu's exit animation.
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
