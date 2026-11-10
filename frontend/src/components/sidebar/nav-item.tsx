"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { NavItem, NavRenderContext } from "./types";
import { TerminalMenuItem, ConsoleMenuItem } from "./session-menu-items";
import { NavItemSpinner } from "./nav-link-status";
import { linkIntentProps } from "@/components/navigation/route-intent";
import { HostHealthCount } from "@/components/host-health/host-health-count";

interface NavItemRowProps {
  item: NavItem;
  ctx: NavRenderContext;
}

// A single leaf row in the expanded sidebar (and inside the collapsed flyout).
// Store-driven items (Terminals/Consoles) delegate to their live menu components.
export function NavItemRow({ item, ctx }: NavItemRowProps) {
  if (item.render === "terminal-menu") return <TerminalMenuItem indent={item.indent} />;
  if (item.render === "console-menu") return <ConsoleMenuItem indent={item.indent} />;

  const href = item.href;
  if (!href) return null;

  const active = href === ctx.activeHref;
  const Icon = item.icon;

  return (
    <Button
      asChild
      variant={active ? "secondary" : "ghost"}
      size="sm"
      className={cn(
        "w-full justify-start text-sm",
        item.indent && "pl-6",
        active && "sidebar-active"
      )}
    >
      <Link
        href={href}
        // No viewport prefetch: every sidebar leaf prefetching on sight would fire one full RSC
        // request per link on each page load, saturating the connection budget and delaying the
        // click the operator made. Warming happens on intent below.
        prefetch={false}
        {...linkIntentProps(ctx.intent, href, ctx.warmFor(item.prefetchKey ?? href))}
        aria-current={active ? "page" : undefined}
      >
        <Icon className="h-4 w-4 mr-2 shrink-0" />
        {item.label}
        {item.badge === "host-health" && <HostHealthCount />}
        <NavItemSpinner />
      </Link>
    </Button>
  );
}
