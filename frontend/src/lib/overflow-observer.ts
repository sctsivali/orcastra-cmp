// Whether a piece of text is currently being clipped by its own box.
//
// CSS has no selector for "this text is overflowing": `text-overflow` exposes no state hook,
// and a container query knows the box width but not the length of the text inside it. So the
// only way to hide a reveal control on a value that genuinely fits is to measure, and the only
// way to measure is at runtime.
//
// important: this is not a public hook. A `useIsOverflowing` in components/ is how the reveal
// primitive gets bypassed and every screen starts inventing its own treatment again, which is
// the drift the primitive exists to end. Only components/ui/overflow-text.tsx calls this.
//
// note: one module-level ResizeObserver rather than one per subscriber. The gain is not raw
// throughput, browsers run a single observation loop per document either way. It is that the
// subscription is keyed on the DOM node rather than on an effect's dependency array. These
// tables re-render on every react-query refetch, and a per-instance effect with the wrong deps
// would observe and unobserve roughly two hundred elements on every poll, forever. It also puts
// the "no ResizeObserver at all" fallback in one place instead of two hundred.

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
  // box, so ResizeObserver stays silent through it. Geist is wired through next/font with a
  // metric-matched fallback so the delta is small, but small is not zero, and a swap that makes
  // text wider AFTER the control was already hidden strands the value with no way to read it.
  // That is the same shape as a reveal gated on a condition that is false exactly when the
  // reveal is needed, which this codebase has shipped before.
  if (!fontsHooked && typeof document !== "undefined" && document.fonts) {
    fontsHooked = true;
    document.fonts.ready.then(remeasureAll).catch(() => undefined);
  }

  return observer;
}

/**
 * Watch `element` and call `callback` whenever its clipped state changes.
 *
 * important: the callback fires only on a CHANGE, and never fires at all when there is no
 * ResizeObserver (jsdom, older Safari). Callers must therefore start from the pessimistic
 * assumption that the text IS clipped and let a measurement take the control away, never the
 * other way round. A control that appears only once JavaScript has decided it is needed is a
 * control that is missing in exactly the window where it is needed.
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
