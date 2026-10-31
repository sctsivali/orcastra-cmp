"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn, plural } from "@/lib/utils";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import type { NavGroup, NavItem, NavRenderContext } from "./types";
import { NavItemRow } from "./nav-item";
import { NavRailIcon } from "./nav-link-status";
import { linkIntentProps } from "@/components/navigation/route-intent";
import { useHostHealthAttention } from "@/components/host-health/host-health-count";

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
            prefetch={false}
            aria-label={item.label}
            aria-current={active ? "page" : undefined}
            {...linkIntentProps(ctx.intent, item.href, ctx.warmFor(item.prefetchKey ?? item.href))}
          >
            <NavRailIcon icon={Icon} />
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  );
}

// A group on the collapsed rail: a single icon that opens a Popover flyout listing the
// group's destinations, so collapsing never hides a page. Opens on hover without stealing
// focus, and on keyboard or click with focus moving into the flyout.
function FlyoutGroup({ group, ctx }: { group: NavGroup; ctx: NavRenderContext }) {
  const [open, setOpen] = React.useState(false);
  const pathname = usePathname();
  const interaction = React.useRef<"mouse" | "keyboard">("mouse");
  const closeTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const terminalCount = useTerminalStore((s) => s.terminals.length);
  const consoleCount = useConsoleStore((s) => s.consoles.length);
  // note: only a group that carries the Host Health item counts it, and that item is admin-only,
  // so the filtered tree never hands a non-admin's group one.
  const attention = useHostHealthAttention(group.items.some((i) => i.badge === "host-health"));
  const sessions = group.id === "compute" ? terminalCount + consoleCount : 0;
  const badge = sessions + attention;

  const openNow = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    setOpen(true);
  };
  // important: the grace period is what lets the pointer cross the gap between the rail
  // icon and the flyout without the panel closing underneath it.
  const closeSoon = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setOpen(false), 120);
  };
  React.useEffect(() => () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }, []);

  // important: pointer events, not mouse events. A tap fires a synthetic mouseenter before
  // click in every mobile browser, so hover-to-open would open the flyout and the trigger's
  // own click would immediately toggle it shut. Ignoring non-mouse pointers lets a tap fall
  // through to Radix's normal click-to-toggle.
  const handlePointerEnter = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    interaction.current = "mouse";
    openNow();
  };
  const handlePointerLeave = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    closeSoon();
  };

  // important: close on route commit, not on click. Closing on click unmounted the <Link>
  // and killed its pending spinner, so the flyout vanished and nothing replaced it until the
  // page arrived. Clicking outside still closes it if a navigation stalls.
  React.useEffect(() => {
    setOpen(false);
  }, [pathname]);

  const groupActive = group.items.some((i) => i.href && i.href === ctx.activeHref);
  const flyItems = group.items.filter((i) => !i.render);
  const Icon = group.icon ?? group.items[0]?.icon;


  const counted = [
    sessions > 0 ? plural(sessions, "active session") : null,
    attention > 0 ? `${plural(attention, "host service needs", "host services need")} attention` : null,
  ].filter(Boolean);
  const triggerLabel = counted.length > 0 ? `${group.label}, ${counted.join(", ")}` : group.label;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant={groupActive ? "secondary" : "ghost"}
          size="icon"
          aria-label={triggerLabel}
          className={cn("relative", groupActive && "sidebar-active")}
          onPointerEnter={handlePointerEnter}
          onPointerLeave={handlePointerLeave}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " " || e.key === "ArrowRight") {
              interaction.current = "keyboard";
            }
          }}
        >
          {Icon ? <Icon className="h-5 w-5" /> : null}
          {badge > 0 && (
            <span
              aria-hidden="true"
              // `sidebar-count-solid`, not `sidebar-count`: this one sits on an icon with no
              // label beside it, so it is the one chip on the rail whose job is to be noticed.
              // The class also exempts it from the sidebar text overrides, which would
              // otherwise repaint its foreground to the ground colour and leave it unreadable
              // on its own accent fill.
              className="absolute -top-1 -right-1 flex h-[18px] min-w-[18px] items-center justify-center rounded px-1 text-xs font-semibold leading-none sidebar-count-solid"
            >
              {badge}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        side="right"
        align="start"
        sideOffset={8}
        aria-label={group.label}
        className="w-56 p-1.5"
        onPointerEnter={handlePointerEnter}
        onPointerLeave={handlePointerLeave}
        onOpenAutoFocus={(e) => {
          // Hover-open must not steal focus; keyboard-open should move into the flyout.
          if (interaction.current === "mouse") e.preventDefault();
        }}
      >
        <p className="px-2 py-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {group.label}
        </p>
        {flyItems.map((item) => (
          <NavItemRow key={item.id} item={{ ...item, indent: false }} ctx={ctx} />
        ))}
      </PopoverContent>
    </Popover>
  );
}

interface CollapsedRailProps {
  groups: NavGroup[];
  ctx: NavRenderContext;
}

// The collapsed (icon-rail) navigation. Pinned items become tooltip icons; titled groups
// become flyout popovers.
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
