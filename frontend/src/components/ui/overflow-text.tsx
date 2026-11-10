"use client";

import * as React from "react";
import { Check, ChevronDown, ChevronUp, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { copyText } from "@/lib/clipboard";
import { observeOverflow } from "@/lib/overflow-observer";
import { cn } from "@/lib/utils";

// One value that is longer than the space it was given: the scalar counterpart of
// components/ui/overflow-cell.tsx, which handles a collection that does not fit a cell.
//
// important: this component never shortens `value`, and callers must not pass a pre-sliced
// string. The whole value stays in the DOM, so find-in-page, text selection and assistive
// technology have it and the clip is purely visual (a `title` would reach hover users only).
// CSS clipping leaves the text in the accessibility tree, so the toggle is named for what it
// does; repeating the value would make a screen reader read a 64 character fingerprint twice.
//
// important: the control is a real <button> in both modes. A span given an onClick or trigger
// props takes no focus, gets no focus ring and ignores Enter.
//
// No motion: two icons instead of one rotating icon, so there is no transform to guard behind
// prefers-reduced-motion.
export interface OverflowTextProps {
  /**
   * The whole value, never pre-sliced and never pre-ellipsised: `value.slice(0, 12)` passed here
   * is a bug.
   */
  value: string;
  /**
   * What the value is, in the operator's words, ex; "fingerprint for ubuntu-24.04".
   * Names both buttons.
   *
   * important: required, as on CopyableText. An icon-only button is a bare "button" in the
   * accessibility tree unless the element itself names it (a wrapping Tooltip adds a
   * description, not a name), and twenty unnamed toggles down one column announce identically.
   * check-icon-button-names.mjs enforces it.
   */
  label: string;
  /**
   * "inline" expands in place, where the container can grow without displacing what the operator
   * watches. "overlay" opens a Popover, for size-locked chrome (a console info panel over a live
   * terminal); Radix tooltips do not open on touch, add a description rather than a name, and
   * cannot hold a copy button.
   *
   * important: neither mode fits clipped text inside a control (SelectTrigger, FilterTrigger, a
   * tray button, DropdownMenuItem): a nested button is invalid markup, competes for the click, and
   * is unreachable in a Radix menu, whose roving focus reaches only menu items. Name the existing
   * control with the whole value and print it in full in the surface it opens.
   */
  mode?: "inline" | "overlay";
  /**
   * Lines kept at rest. 1 clips an identifier to a single line. 2 and 3 clamp prose such as a
   * warning message or a project description. "none" never clips: the value always wraps, for a
   * surface with room to show it, such as a detail dialog.
   *
   * 1 uses `truncate`, which refuses to wrap, as a value in a dense grid wants; `line-clamp-1`
   * would let the text wrap and then hide everything below the first line.
   */
  lines?: 1 | 2 | 3 | "none";
  /** Monospace at rest and revealed. `font-mono` on the span, never a <code> element. */
  mono?: boolean;
  /**
   * Characters kept visible at rest on one line, ex; 12 for a certificate fingerprint, which is
   * what `lxc config trust list` prints, so the two can be matched by eye.
   *
   * important: a CSS width; nothing is cut. The span is capped at exactly this many `ch` and the
   * whole value stays in the DOM. The clip carries no ellipsis: the trust list prints its 12
   * characters bare, the reveal control beside the value already says there is more, and the
   * ellipsis glyph is wider than one cell in the house mono face, so it would eat the twelfth
   * character. Only meaningful with `mono` and `lines={1}`: `ch` is a glyph width only in a
   * monospace face.
   */
  visibleChars?: number;
  /**
   * When the copy button appears. "expanded" costs nothing at rest, so a table keeps its rows
   * per screen. "always" is the detail-field preset, where copying the value is the reason the
   * field is on screen.
   */
  copy?: "expanded" | "always" | "none";
  /**
   * "auto" measures and drops the control when the value fits. "always" skips measurement for
   * a value that is opaque, fixed length and never fits, ex; a 64 character fingerprint or a
   * UUID.
   *
   * "auto" starts pessimistic, so "always" is only an optimisation. If a control is missing,
   * the measurement is the bug.
   */
  reveal?: "auto" | "always";
  /**
   * Which surface the control is measured against. "surface" is the themed app and reads
   * --muted-foreground. "chrome" is the console and terminal shell, which paints its own fixed
   * dark background and does not follow the theme, so a token tuned against --card is the wrong
   * measurement there. check-semantic-colour.mjs exempts that tree by path for the same reason.
   */
  tone?: "surface" | "chrome";
  /** Reports the copy result; the container owns the live region. */
  onCopied?: (ok: boolean) => void;
  className?: string;
  /**
   * A decorated rendering of the same value, ex; a highlighted search match, or a TableLink that
   * drills into the record. Pass a function to receive the expanded state, which a decorated
   * rendering needs when it does its own clipping: TableLink wraps its text in a `truncate` span,
   * so it has to be told to stop, or the toggle beside it would expand a box whose contents are
   * still clipped one element further in.
   *
   * important: it must render `value` in full.
   */
  children?: React.ReactNode | ((expanded: boolean) => React.ReactNode);
}

// useLayoutEffect on the client, useEffect on the server. The measurement has to land before
// the browser paints or a short value shows a control for one frame, but React logs a warning
// for useLayoutEffect during server rendering and a client component is still server rendered.
const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? React.useLayoutEffect : React.useEffect;

const COPIED_FOR_MS = 1500;

export function OverflowText({
  value,
  label,
  mode = "inline",
  lines = 1,
  mono,
  visibleChars,
  copy = "expanded",
  reveal = "auto",
  tone = "surface",
  onCopied,
  className,
  children,
}: OverflowTextProps) {
  const valueId = React.useId();
  const spanRef = React.useRef<HTMLSpanElement>(null);
  const [expanded, setExpanded] = React.useState(false);
  const [copied, setCopied] = React.useState(false);

  // important: pessimistic. The control is present until a measurement proves the value fits,
  // so a browser with no ResizeObserver and the render before hydration both keep the reveal,
  // and the server HTML matches the first client render.
  const [overflowing, setOverflowing] = React.useState(true);

  const clamped = lines !== "none" && !expanded;
  const axis: "x" | "y" = lines === 1 ? "x" : "y";
  const shouldMeasure = lines !== "none" && reveal === "auto" && !expanded;

  useIsomorphicLayoutEffect(() => {
    if (!shouldMeasure) return;
    const node = spanRef.current;
    if (!node) return;
    return observeOverflow(node, axis, setOverflowing);
  }, [shouldMeasure, axis, value]);

  React.useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), COPIED_FOR_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const handleCopy = async () => {
    const ok = await copyText(value);
    setCopied(ok);
    onCopied?.(ok);
  };

  const showToggle = lines !== "none" && (overflowing || expanded);
  const showCopy = copy === "always" || (copy === "expanded" && (expanded || lines === "none"));

  const buttonTone =
    tone === "chrome"
      ? "text-gray-400 hover:bg-white/10 hover:text-gray-100 focus-visible:bg-white/10"
      : "text-muted-foreground hover:text-foreground focus-visible:bg-accent";

  // 24px is the target size WCAG 2.5.8 asks for, and -my-1 keeps it from growing the 20px line
  // box a table row is built on, so a column of these costs no rows per screen.
  //
  // Only where something is clipped. lines="none" is the detail-surface preset, not a table
  // row, and the negative margin there would tighten the gap between a dialog's header and its
  // first section.
  const buttonClass = cn("h-6 w-6 shrink-0", lines !== "none" && "-my-1", buttonTone);

  const valueClass = cn(
    "min-w-0",
    mono && "font-mono",
    clamped
      ? lines === 1
        ? "truncate"
        : lines === 2
          ? "line-clamp-2"
          : "line-clamp-3"
      : cn("whitespace-normal", mono ? "break-all" : "break-words"),
  );

  const valueStyle: React.CSSProperties | undefined =
    clamped && lines === 1 && mono && visibleChars
      ? { maxWidth: `${visibleChars}ch`, textOverflow: "clip" }
      : undefined;

  const copyButton = showCopy ? (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className={buttonClass}
      onClick={handleCopy}
      aria-label={`Copy ${label}`}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
      ) : (
        <Copy className="h-3.5 w-3.5" aria-hidden="true" />
      )}
    </Button>
  ) : null;

  const renderValue = (open: boolean): React.ReactNode =>
    typeof children === "function" ? children(open) : (children ?? value);

  const rootClass = cn(
    "inline-flex min-w-0 max-w-full gap-1 align-middle",
    expanded ? "items-start" : "items-center",
    className,
  );

  if (mode === "overlay") {
    // Nothing is clipped, so no control. A Popover whose trigger never renders would also be
    // a Radix root with no anchor.
    if (!showToggle) {
      return (
        <span className={rootClass} data-slot="overflow-text">
          <span ref={spanRef} className={valueClass} style={valueStyle}>
            {renderValue(false)}
          </span>
        </span>
      );
    }
    return (
      <Popover>
        <span className={rootClass} data-slot="overflow-text">
          <span ref={spanRef} className={valueClass} style={valueStyle}>
            {renderValue(false)}
          </span>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={buttonClass}
              data-reveal-trigger=""
              aria-label={`Show full ${label}`}
            >
              <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
            </Button>
          </PopoverTrigger>
        </span>
        <PopoverContent align="start" className="w-72">
          <div className="flex items-start gap-2">
            <span className={cn("min-w-0 break-all text-sm", mono && "font-mono")}>{value}</span>
            {copy === "none" ? null : (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-6 w-6 shrink-0 text-muted-foreground hover:text-foreground"
                onClick={handleCopy}
                aria-label={`Copy ${label}`}
              >
                {copied ? (
                  <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
                ) : (
                  <Copy className="h-3.5 w-3.5" aria-hidden="true" />
                )}
              </Button>
            )}
          </div>
        </PopoverContent>
      </Popover>
    );
  }

  return (
    <span className={rootClass} data-slot="overflow-text" data-expanded={expanded ? "" : undefined}>
      <span ref={spanRef} id={valueId} className={valueClass} style={valueStyle}>
        {renderValue(expanded)}
      </span>
      {showToggle ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className={buttonClass}
          data-reveal-trigger=""
          // important: both ends of this relationship are owned here. A caller-supplied or
          // Radix-injected aria-controls can point at an id no element carries (see
          // filter-trigger.tsx).
          aria-expanded={expanded}
          aria-controls={valueId}
          onClick={() => setExpanded((open) => !open)}
          aria-label={expanded ? `Hide full ${label}` : `Show full ${label}`}
        >
          {expanded ? (
            <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
          )}
        </Button>
      ) : null}
      {copyButton}
    </span>
  );
}
