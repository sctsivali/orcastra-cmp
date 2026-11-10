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
import type { CreateAction } from "./create-menu-config";

interface CreateMenuProps {
  /** Already filtered by role and by the current route. Empty means render nothing. */
  actions: CreateAction[];
  isReadOnly: boolean;
}

const READ_ONLY_REASON = "Disabled in read-only mode";

/**
 * The console's only global create control: one trigger at every width, in the sticky header.
 *
 * No floating action button: a fixed overlay covers other controls on a phone and can hide the
 * control a keyboard user just focused (WCAG 2.2 SC 2.4.11). The sticky header is always
 * reachable and covers nothing.
 *
 * important: a read-only item is marked with aria-disabled, never with Radix's `disabled` prop.
 * @radix-ui/react-menu drops a disabled item from roving focus and typeahead, and the house item
 * class adds data-[disabled]:pointer-events-none, so it could never show why it is unavailable.
 */
export function CreateMenu({ actions, isReadOnly }: CreateMenuProps) {
  const reasonId = React.useId();

  if (actions.length === 0) return null;

  const trigger = (
    <Button className="gap-1.5 px-3 shadow-lg sm:gap-2 sm:px-4">
      <Plus className="h-4 w-4" aria-hidden="true" />
      <span>New</span>
      <ChevronDown className="h-4 w-4" aria-hidden="true" />
    </Button>
  );

  const menu = (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      {/* The shared DropdownMenuContent animates with no reduced-motion guard. Fixing the
          primitive would touch every menu in the app, so the override is scoped here. */}
      <DropdownMenuContent align="end" side="bottom" className="w-56 motion-reduce:animate-none">
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
                // anchor from navigating, so there must be no href at all.
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

  return menu;
}
