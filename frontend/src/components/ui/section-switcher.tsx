"use client";

import * as React from "react";
import { AlertTriangle, type LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface SectionSwitcherItem<S extends string> {
  id: S;
  label: string;
  icon?: LucideIcon;
  /** How many rows the section lists; absent until it has been read. */
  count?: number;
  /** Rows that need someone, ex; organizations whose owner key stopped working. */
  attention?: { count: number; label: string };
}

/**
 * The sections of one settings tab, as links under the tab bar.
 *
 * important: links with aria-current, not a second tablist. Each section has its own URL
 * (?section=), opens in a new tab on a modifier click and is walked by Back, which is navigation;
 * and a nested role="tab" set inside the Settings tabpanel is what the harness counts as a tab.
 * Underlined rather than pilled, so it reads as a level below the Settings tabs above it.
 */
export function SectionSwitcher<S extends string>({
  label,
  items,
  active,
  hrefFor,
  onSelect,
  className,
  navRef,
}: {
  label: string;
  items: SectionSwitcherItem<S>[];
  active: S;
  hrefFor: (section: S) => string;
  onSelect: (section: S) => void;
  className?: string;
  navRef?: React.Ref<HTMLElement>;
}) {
  return (
    <nav ref={navRef} aria-label={label} className={cn("max-w-full overflow-x-auto border-b scroll-mt-20", className)}>
      <ul className="flex w-max items-end gap-1">
        {items.map((item) => {
          const current = item.id === active;
          const Icon = item.icon;
          return (
            <li key={item.id}>
              <a
                href={hrefFor(item.id)}
                aria-current={current ? "true" : undefined}
                data-section={item.id}
                onClick={(event) => {
                  // A modifier or middle click keeps the browser's own behaviour: a new tab.
                  if (event.defaultPrevented || event.button !== 0) return;
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  onSelect(item.id);
                }}
                className={cn(
                  "relative -mb-px inline-flex min-h-10 items-center gap-2 whitespace-nowrap rounded-t-md border-b-2 px-3 py-2 text-sm font-medium transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  current
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                )}
              >
                {Icon ? <Icon className="h-4 w-4 shrink-0" aria-hidden="true" /> : null}
                {item.label}
                {item.count !== undefined ? (
                  <Badge asChild variant="muted" className="px-1.5 py-0 font-normal tabular-nums">
                    <span>
                      {item.count}
                      <span className="sr-only">{item.count === 1 ? " item" : " items"}</span>
                    </span>
                  </Badge>
                ) : null}
                {item.attention && item.attention.count > 0 ? (
                  <Badge asChild variant="caution" className="gap-1 px-1.5 py-0 tabular-nums">
                    <span>
                      <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                      {item.attention.count}
                      <span className="sr-only"> {item.attention.label}</span>
                    </span>
                  </Badge>
                ) : null}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/**
 * The sections a tab has opened so far. A section mounts on its first visit and then stays
 * mounted while hidden, so its search, filters, page and unsaved edits survive a switch.
 */
export function useVisitedSections<S extends string>(active: S): ReadonlySet<S> {
  const [visited, setVisited] = React.useState<ReadonlySet<S>>(() => new Set([active]));
  if (!visited.has(active)) {
    // note: derived during render, React's documented pattern for state that follows a prop;
    // an effect would paint the new section blank for one frame first.
    const next = new Set(visited);
    next.add(active);
    setVisited(next);
  }
  return visited;
}

/**
 * Keep keyboard and overlay state honest when Back or Forward changes the section under them.
 *
 * A click on the switcher cannot happen with a dialog open (the dialog is modal), but Back can.
 * Radix renders dialogs and menus into a portal on body, so hiding the section that opened one
 * would leave it floating over the next section. Escape is what closes a Radix layer, so it is
 * sent while one is open; then focus moves to the active section link if it was inside a section
 * that is now hidden, and the switcher is brought into view if the page had scrolled past it.
 */
export function useSectionChangeEffects(active: string, navRef: React.RefObject<HTMLElement | null>) {
  const previous = React.useRef(active);
  React.useEffect(() => {
    if (previous.current === active) return;
    previous.current = active;
    for (let i = 0; i < 3; i += 1) {
      const open = document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"], [role="menu"][data-state="open"], [role="listbox"][data-state="open"]');
      if (!open) break;
      open.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    }
    const nav = navRef.current;
    if (!nav) return;
    const focused = document.activeElement;
    const hiddenFocus = focused instanceof HTMLElement && !!focused.closest("section[id^='section-'].hidden");
    if (hiddenFocus) {
      nav.querySelector<HTMLElement>('a[aria-current="true"]')?.focus({ preventScroll: true });
    }
    if (nav.getBoundingClientRect().top < 0) {
      const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
      nav.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    }
  }, [active, navRef]);
}

/** One section's content: mounted once visited, hidden (not unmounted) while another is active. */
export function SectionPanel({
  id,
  active,
  visited,
  label,
  children,
}: {
  id: string;
  active: boolean;
  visited: boolean;
  label: string;
  children: React.ReactNode;
}) {
  if (!visited) return null;
  return (
    <section id={`section-${id}`} aria-label={label} className={cn("space-y-6", !active && "hidden")}>
      {children}
    </section>
  );
}
