"use client";

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";
import { cn } from "@/lib/utils";
import { useDelayedFlag } from "@/hooks/use-delayed-flag";

interface NavigationProgressValue {
  pending: boolean;
  navigate: (href: string, options?: { replace?: boolean }) => void;
}

const NavigationProgressContext = React.createContext<NavigationProgressValue>({
  pending: false,
  navigate: () => {},
});

export function useNavigationProgress(): NavigationProgressValue {
  return React.useContext(NavigationProgressContext);
}

// Longest a navigation may hold the bar before we assume it was cancelled: blocked by
// middleware, superseded by another click, or swallowed by an error boundary.
const WATCHDOG_MS = 10_000;
const SHOW_AFTER_MS = 150;

function NavigationProgressBar({ pending }: { pending: boolean }) {
  const visible = useDelayedFlag(pending, SHOW_AFTER_MS);
  const [scale, setScale] = React.useState(0);
  const hasShown = React.useRef(false);

  React.useEffect(() => {
    if (visible) {
      hasShown.current = true;
      // Three coarse steps interpolated by the CSS transition. No rAF loop, and nothing
      // that can freeze mid-animation when the transition is disabled.
      setScale(0.3);
      const mid = setTimeout(() => setScale(0.65), 350);
      const tail = setTimeout(() => setScale(0.88), 1200);
      return () => {
        clearTimeout(mid);
        clearTimeout(tail);
      };
    }
    if (!hasShown.current) return;
    setScale(1);
    const reset = setTimeout(() => {
      setScale(0);
      hasShown.current = false;
    }, 220);
    return () => clearTimeout(reset);
  }, [visible]);

  return (
    <>
      <div
        aria-hidden="true"
        className={cn(
          "pointer-events-none fixed inset-x-0 top-0 z-[60] h-0.5",
          "transition-opacity duration-200 motion-reduce:transition-none",
          visible ? "opacity-100" : "opacity-0"
        )}
      >
        {/* note: scaleX, not width, so the bar animates on the compositor and never forces
            layout against the sticky header underneath it. */}
        <div
          className="h-full w-full origin-left bg-primary transition-transform duration-300 ease-out motion-reduce:transition-none"
          style={{ transform: `scaleX(${scale})` }}
        />
      </div>
      <div role="status" aria-live="polite" className="sr-only">
        {visible ? "Loading page" : ""}
      </div>
    </>
  );
}

/**
 * Route-change feedback for the whole console.
 *
 * Two independent signals feed one bar, because neither covers the other:
 *  - anchor clicks, caught by a capture-phase document listener, which covers every
 *    <Link> in the app with no call-site changes;
 *  - programmatic navigation, exposed as navigate(), since <Link> uses the global
 *    React.startTransition internally and so never sets a useTransition isPending.
 *
 * Both end on usePathname() changing, which is the exact commit that paints the new route.
 */
export function NavigationProgressProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [linkPending, setLinkPending] = React.useState(false);
  const [transitionPending, startTransition] = React.useTransition();
  const pending = linkPending || transitionPending;

  React.useEffect(() => {
    setLinkPending(false);
  }, [pathname]);

  React.useEffect(() => {
    if (!linkPending) return;
    const timer = setTimeout(() => setLinkPending(false), WATCHDOG_MS);
    return () => clearTimeout(timer);
  }, [linkPending]);

  React.useEffect(() => {
    // important: capture phase. <Link> calls preventDefault() on this same click, so a
    // bubble-phase listener could not tell a handled navigation from a cancelled one.
    function handleClick(event: MouseEvent) {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }
      const target = event.target as HTMLElement | null;
      const anchor = target?.closest?.("a");
      if (!anchor || anchor.target === "_blank" || anchor.hasAttribute("download")) return;
      const raw = anchor.getAttribute("href");
      if (!raw || raw.startsWith("#")) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      // Same destination means no commit, so nothing would ever clear the bar.
      if (url.pathname === window.location.pathname && url.search === window.location.search) {
        return;
      }
      setLinkPending(true);
    }
    document.addEventListener("click", handleClick, true);
    return () => document.removeEventListener("click", handleClick, true);
  }, []);

  const navigate = React.useCallback(
    (href: string, options?: { replace?: boolean }) => {
      startTransition(() => {
        if (options?.replace) router.replace(href);
        else router.push(href);
      });
    },
    [router]
  );

  const value = React.useMemo(() => ({ pending, navigate }), [pending, navigate]);

  return (
    <NavigationProgressContext.Provider value={value}>
      {children}
      <NavigationProgressBar pending={pending} />
    </NavigationProgressContext.Provider>
  );
}
