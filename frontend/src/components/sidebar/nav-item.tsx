"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { NavItem, NavRenderContext } from "./types";
import { TerminalMenuItem, ConsoleMenuItem } from "./session-menu-items";

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
        // Force a full route prefetch (RSC + JS chunk). These routes are dynamic (middleware auth +
        // client data fetch), so the default "auto" under-prefetches without a loading.tsx and the
        // page loads at click time. This warms the route shell so a click commits near-instantly;
        // data is still warmed on hover below. Prefetches the shell only, not the API data.
        prefetch={true}
        onClick={ctx.onLinkClick}
        onMouseEnter={() => ctx.prefetch(item.prefetchKey ?? href)}
        aria-current={active ? "page" : undefined}
      >
        <Icon className="h-4 w-4 mr-2 shrink-0" />
        {item.label}
      </Link>
    </Button>
  );
}
