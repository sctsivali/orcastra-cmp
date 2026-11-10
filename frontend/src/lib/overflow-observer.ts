// Whether a piece of text is currently being clipped by its own box.
//
// CSS has no selector for overflowing text (`text-overflow` exposes no state, and a container
// query knows the box width but not the text length), so hiding a reveal control on a value
// that fits takes a runtime measurement.
//
// Not a public hook. Only components/ui/overflow-text.tsx calls this; a `useIsOverflowing`
// in components/ would let screens bypass the reveal primitive.
//
// One module-level ResizeObserver keys each subscription on its DOM node rather than on an
// effect's dependency array, so tables that re-render on every refetch do not observe and
// unobserve hundreds of elements per poll. The no-ResizeObserver fallback lives here once.

type OverflowCallback = (overflowing: boolean) => void;

type Axis = "x" | "y";

interface Subscription {
  element: HTMLElement;
  axis: Axis;
  callback: OverflowCallback;
  last: boolean | null;
}

const subscriptions = new Map<Element, Subscription>();

let observer: ResizeObserver | null = null;
let fontsHooked = false;

// A single pixel of slack. Sub-pixel line-height and letter-spacing rounding routinely leaves
// scrollWidth a fraction above clientWidth on text that visibly fits, and without the slack
// every such value would carry a control that reveals nothing.
const SLACK = 1;

function isOverflowing(subscription: Subscription): boolean {
  const el = subscription.element;
  return subscription.axis === "y"
    ? el.scrollHeight - el.clientHeight > SLACK
    : el.scrollWidth - el.clientWidth > SLACK;
}

function report(subscription: Subscription): void {
  const next = isOverflowing(subscription);
  if (next === subscription.last) return;
  subscription.last = next;
  subscription.callback(next);
}

/** Re-measure every subscriber. Exported for the font-swap hook below and for tests. */
export function remeasureAll(): void {
  subscriptions.forEach(report);
}

function ensureObserver(): ResizeObserver | null {
  if (observer) return observer;
  if (typeof ResizeObserver === "undefined") return null;

  observer = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const subscription = subscriptions.get(entry.target);
      if (subscription) report(subscription);
    }
  });

  // important: a font swap changes how wide the text renders without changing the element's
  // box, so ResizeObserver stays silent. Geist's metric-matched fallback keeps the delta small
  // but not zero, and text that widens after the control was hidden would be left unreadable.
  if (!fontsHooked && typeof document !== "undefined" && document.fonts) {
    fontsHooked = true;
    document.fonts.ready.then(remeasureAll).catch(() => undefined);
  }

  return observer;
}

/**
 * Watch `element` and call `callback` whenever its clipped state changes.
 *
 * important: the callback fires only on a change, and never when there is no ResizeObserver
 * (jsdom, older Safari). Callers must start by assuming the text is clipped and let a
 * measurement remove the control, never the other way round.
 */
export function observeOverflow(
  element: HTMLElement,
  axis: Axis,
  callback: OverflowCallback,
): () => void {
  const ro = ensureObserver();
  const subscription: Subscription = { element, axis, callback, last: null };
  subscriptions.set(element, subscription);

  // Measure once up front. ResizeObserver does deliver an initial observation, but only on the
  // next frame, and the caller wants an answer before the browser paints.
  report(subscription);
  ro?.observe(element);

  return () => {
    subscriptions.delete(element);
    ro?.unobserve(element);
  };
}
