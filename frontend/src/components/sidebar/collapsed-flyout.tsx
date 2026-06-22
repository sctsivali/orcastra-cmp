"use client";

import * as React from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import type { NavGroup, NavItem, NavRenderContext } from "./types";
import { NavItemRow } from "./nav-item";

// A pinned leaf icon on the collapsed rail: icon-only link with a tooltip label.
function RailLeaf({ item, ctx }: { item: NavItem; ctx: NavRenderContext }) {
  if (item.render || !item.href) return null;
  const active = item.href === ctx.activeHref;
  const Icon = item.icon;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          asChild
          variant={active ? "secondary" : "ghost"}
          size="icon"
          className={cn(active && "sidebar-active")}
        >
          <Link
            href={item.href}
            prefetch={true}
            aria-label={item.label}
            aria-current={active ? "page" : undefined}
            onClick={ctx.onLinkClick}
            onMouseEnter={() => ctx.prefetch(item.prefetchKey ?? item.href!)}
          >
            <Icon className="h-5 w-5" />
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  );
}

// A group on the collapsed rail: a single icon that opens a Popover flyout listing
// the group's destinations (so everything stays reachable while collapsed). Opens on
// hover (no focus steal) and on keyboard/click (focus moves into the flyout).
function FlyoutGroup({ group, ctx }: { group: NavGroup; ctx: NavRenderContext }) {
  const [open, setOpen] = React.useState(false);
  const interaction = React.useRef<"mouse" | "keyboard">("mouse");
  const closeTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const terminalCount = useTerminalStore((s) => s.terminals.length);
  const consoleCount = useConsoleStore((s) => s.consoles.length);
  const badge = group.id === "compute" ? terminalCount + consoleCount : 0;

  const openNow = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    setOpen(true);
  };
  const closeSoon = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setOpen(false), 120);
  };
  React.useEffect(() => () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }, []);

  const groupActive = group.items.some((i) => i.href && i.href === ctx.activeHref);
  const flyItems = group.items.filter((i) => !i.render);
  const Icon = group.icon ?? group.items[0]?.icon;

  // Inside the flyout, close it after navigating.
  const flyoutCtx: NavRenderContext = {
    ...ctx,
    onLinkClick: () => {
      setOpen(false);
      ctx.onLinkClick();
    },
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant={groupActive ? "secondary" : "ghost"}
          size="icon"
          aria-label={group.label}
          className={cn("relative", groupActive && "sidebar-active")}
          onMouseEnter={() => {
            interaction.current = "mouse";
            openNow();
          }}
          onMouseLeave={closeSoon}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " " || e.key === "ArrowRight") {
              interaction.current = "keyboard";
            }
          }}
        >
          {Icon ? <Icon className="h-5 w-5" /> : null}
          {badge > 0 && (
            <span className="absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[10px] font-semibold text-primary-foreground">
              {badge}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        side="right"
        align="start"
        sideOffset={8}
        className="w-56 p-1.5"
        onMouseEnter={openNow}
        onMouseLeave={closeSoon}
        onOpenAutoFocus={(e) => {
          // Hover-open must not steal focus; keyboard-open should move into the flyout.
          if (interaction.current === "mouse") e.preventDefault();
        }}
      >
        <p className="px-2 py-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {group.label}
        </p>
        {flyItems.map((item) => (
          <NavItemRow key={item.id} item={item} ctx={flyoutCtx} />
        ))}
      </PopoverContent>
    </Popover>
  );
}

interface CollapsedRailProps {
  groups: NavGroup[];
  ctx: NavRenderContext;
}

// The collapsed (icon-rail) navigation. Pinned items become tooltip icons; titled
// groups become flyout popovers.
export function CollapsedRail({ groups, ctx }: CollapsedRailProps) {
  return (
    <div className="flex flex-col items-center gap-1 px-2 py-2">
      {groups.map((group) =>
        group.label ? (
          <FlyoutGroup key={group.id} group={group} ctx={ctx} />
        ) : (
          group.items.map((item) => <RailLeaf key={item.id} item={item} ctx={ctx} />)
        )
      )}
    </div>
  );
}
