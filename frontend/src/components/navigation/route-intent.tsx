"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

// Just under Next's own 300s reuse window for a FULL prefetch entry (STATIC_STALETIME_MS in
// router-reducer/prefetch-cache-utils), so a re-hover re-warms shortly before the cached
// payload expires rather than on every pass over the item.
const TTL_MS = 280_000;
// Long enough that sweeping the pointer down the sidebar warms nothing, short enough that a
// deliberate hover still beats the click by a wide margin.
const DELAY_MS = 90;

export interface RouteIntent {
  /** Pointer settled or focus landed: arm a prefetch. */
  onIntent: (href: string, warm?: () => void) => void;
  /** Pointer left or focus moved before the delay elapsed: disarm. */
  onIntentCancel: () => void;
  /** Pointer down or Enter: fire now, the click is milliseconds away. */
  onIntentCommit: (href: string, warm?: () => void) => void;
}

const NOOP_INTENT: RouteIntent = {
  onIntent: () => {},
  onIntentCancel: () => {},
  onIntentCommit: () => {},
};

const RouteIntentContext = React.createContext<RouteIntent>(NOOP_INTENT);

export function useRouteIntent(): RouteIntent {
  return React.useContext(RouteIntentContext);
}

/**
 * App-wide intent-driven route prefetching.
 *
 * Replaces blanket viewport prefetching, which fired one full RSC request per visible link
 * on every page load and starved the click the operator actually made. One provider means
 * one shared debounce timer, so at most one prefetch is ever in flight no matter how many
 * links are on screen, and one shared TTL map, so a warmed route is never re-warmed.
 */
export function RouteIntentProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastFiredAt = React.useRef(new Map<string, number>());

  const run = React.useCallback(
    (href: string, warm?: () => void) => {
      // important: never warm the route already on screen. It is a guaranteed-wasted RSC
      // request and a wasted slot in the browser's per-origin connection budget.
      // note: an href carrying its own query string is a distinct destination, so it compares
      // against the full URL. The severity cards all point at /inventory/warnings and differ
      // only by search. A bare href compares against pathname alone, otherwise standing on a
      // filtered table (use-table-filters writes the filter into the URL) would make the
      // sidebar entry for that same table look like somewhere else.
      const here = window.location;
      const isHere = href.includes("?")
        ? href === here.pathname + here.search
        : href === here.pathname;
      if (isHere) return;
      const previous = lastFiredAt.current.get(href);
      if (previous !== undefined && Date.now() - previous < TTL_MS) return;
      lastFiredAt.current.set(href, Date.now());
      // note: on the legacy prefetch path (experimental.clientSegmentCache off, which is the
      // default and what next.config.js uses) router.prefetch defaults to PrefetchKind.FULL,
      // the same payload the old prefetch={true} produced. If clientSegmentCache is ever
      // enabled this silently downgrades to a partial prefetch.
      router.prefetch(href);
      warm?.();
    },
    [router]
  );

  const onIntentCancel = React.useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const onIntent = React.useCallback(
    (href: string, warm?: () => void) => {
      onIntentCancel();
      timer.current = setTimeout(() => {
        timer.current = null;
        run(href, warm);
      }, DELAY_MS);
    },
    [onIntentCancel, run]
  );

  const onIntentCommit = React.useCallback(
    (href: string, warm?: () => void) => {
      onIntentCancel();
      run(href, warm);
    },
    [onIntentCancel, run]
  );

  React.useEffect(() => onIntentCancel, [onIntentCancel]);

  const value = React.useMemo(
    () => ({ onIntent, onIntentCancel, onIntentCommit }),
    [onIntent, onIntentCancel, onIntentCommit]
  );

  return <RouteIntentContext.Provider value={value}>{children}</RouteIntentContext.Provider>;
}

/**
 * Event props that turn a <Link> into an intent-prefetched link.
 *
 * Covers all three input modes, because prefetch={false} disables Next's own hover and
 * touch handlers and this becomes the only prefetch path:
 *  - mouse: pointer enter arms after a debounce, pointer leave disarms;
 *  - touch and pen: pointer down fires immediately, the same head start Next's own
 *    onTouchStart gave (finger-down to click is typically 100-300ms);
 *  - keyboard: focus arms, blur disarms, Enter fires immediately.
 */
export function linkIntentProps(intent: RouteIntent, href: string, warm?: () => void) {
  return {
    // important: pointerType guard. A tap fires a synthetic mouseenter in every mobile
    // browser, which would warm a route the user never opens.
    onPointerEnter: (event: React.PointerEvent) => {
      if (event.pointerType === "mouse") intent.onIntent(href, warm);
    },
    onPointerLeave: (event: React.PointerEvent) => {
      if (event.pointerType === "mouse") intent.onIntentCancel();
    },
    onPointerDown: () => intent.onIntentCommit(href, warm),
    onFocus: () => intent.onIntent(href, warm),
    onBlur: () => intent.onIntentCancel(),
    onKeyDown: (event: React.KeyboardEvent) => {
      if (event.key === "Enter") intent.onIntentCommit(href, warm);
    },
  };
}

type IntentLinkProps = Omit<React.ComponentProps<typeof Link>, "prefetch" | "href"> & {
  href: string;
};

/** A <Link> that warms on intent instead of on sight. Drop-in for a plain <Link>. */
export function IntentLink({ href, children, ...rest }: IntentLinkProps) {
  const intent = useRouteIntent();
  return (
    <Link href={href} prefetch={false} {...linkIntentProps(intent, href)} {...rest}>
      {children}
    </Link>
  );
}
