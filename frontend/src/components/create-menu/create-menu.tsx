"use client";

import * as React from "react";
import { ChevronDown, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { IntentLink } from "@/components/navigation/route-intent";
import { cn } from "@/lib/utils";
import type { CreateAction } from "./create-menu-config";

interface CreateMenuProps {
  /** "bar" is the desktop header button, "fab" the mobile floating action button. */
  variant: "bar" | "fab";
  /** Already filtered by role and by the current route. Empty means render nothing. */
  actions: CreateAction[];
  isReadOnly: boolean;
}

const READ_ONLY_REASON = "Disabled in read-only mode";

/**
 * The console's only global create affordance, rendered from one action list so the desktop
 * menu and the mobile button cannot offer different things.
 *
 * important: a read-only item is marked with aria-disabled, never with Radix's `disabled` prop.
 * @radix-ui/react-menu passes `focusable: !disabled` to the roving-focus item and filters
 * disabled items out of the collections that drive arrow keys and typeahead, and the house item
 * class adds data-[disabled]:pointer-events-none on top. A `disabled` item is therefore
 * unreachable by keyboard and emits no hover, so the reason it is unavailable could never be
 * surfaced to anyone. Here the item stays focusable and explains itself instead.
 */
export function CreateMenu({ variant, actions, isReadOnly }: CreateMenuProps) {
  const reasonId = React.useId();

  if (actions.length === 0) return null;

  const trigger =
    variant === "bar" ? (
      <Button className="gap-2 shadow-lg">
        <Plus className="h-4 w-4" aria-hidden="true" />
        <span>New</span>
        <ChevronDown className="h-4 w-4" aria-hidden="true" />
      </Button>
    ) : (
      <Button
        size="lg"
        className="h-14 w-14 rounded-full shadow-lg flex items-center justify-center"
      >
        <Plus className="h-6 w-6 shrink-0" aria-hidden="true" />
        <span className="sr-only">New</span>
      </Button>
    );

  const menu = (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      {/* note: the shared DropdownMenuContent animates in and out with no reduced-motion guard.
          Fixing the primitive would touch every menu in the app, so the override is scoped here. */}
      <DropdownMenuContent
        align="end"
        side={variant === "fab" ? "top" : "bottom"}
        className={cn("w-56 motion-reduce:animate-none", variant === "fab" && "mb-2")}
      >
        {isReadOnly && (
          <>
            <DropdownMenuLabel
              id={reasonId}
              className="px-2 pb-2 pt-1.5 text-xs font-normal leading-snug text-popover-foreground"
            >
              Read-only mode. Creating is disabled.
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
          </>
        )}
        {actions.map((action, index) => {
          const Icon = action.icon;
          const body = (
            <>
              <Icon className="mr-2 h-4 w-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{action.label}</span>
            </>
          );

          return (
            <React.Fragment key={action.id}>
              {index > 0 && action.startsGroup && <DropdownMenuSeparator />}
              {isReadOnly ? (
                // important: no IntentLink in this branch. onSelect is Radix's own select
                // event; preventing it stops the menu closing but would not stop an underlying
                // anchor from navigating, so the href must simply not exist.
                <DropdownMenuItem
                  aria-disabled="true"
                  aria-describedby={reasonId}
                  title={READ_ONLY_REASON}
                  className="cursor-not-allowed opacity-60"
                  onSelect={(event) => event.preventDefault()}
                >
                  {body}
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem asChild className="cursor-pointer">
                  <IntentLink href={action.href}>{body}</IntentLink>
                </DropdownMenuItem>
              )}
            </React.Fragment>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );

  if (variant === "fab") {
    return <div className="sm:hidden fixed bottom-6 right-6 z-50">{menu}</div>;
  }
  return menu;
}
