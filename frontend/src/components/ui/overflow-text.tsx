"use client";

import * as React from "react";
import { Check, ChevronDown, ChevronUp, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { copyText } from "@/lib/clipboard";
import { observeOverflow } from "@/lib/overflow-observer";
import { cn } from "@/lib/utils";

// One value that is longer than the space it was given.
//
// It is the scalar counterpart of components/ui/overflow-cell.tsx: that one is a COLLECTION
// that does not fit a cell, this one is a single value that does not fit its box. The two
// replace the shapes this codebase grew independently in roughly ninety places.
//
// The first shape sliced the string in JavaScript and glued three dots on the end. That does
// not shorten a value, it destroys it: truncateFingerprint removed 52 of a fingerprint's 64
// characters before the string ever reached the DOM, so no selection, no copy, no find-in-page
// and no screen reader could recover them. Six further sites did the same and appended nothing
// at all, leaving the operator no way to even tell the value had been cut.
//
// The second shape clipped with CSS and hung the whole value on a `title` attribute. A `title`
// opens on hover and on nothing else, so no keyboard user and no touch user has ever seen one.
// components/ui/overflow-cell.tsx already condemns that pattern; this component is what the
// call sites reach for instead.
//
// important: this component NEVER shortens `value`. The whole string is always in the DOM, so
// find-in-page, text selection and assistive technology have it at rest and the clip is purely
// visual. CSS clipping does not remove text from the accessibility tree, which is why the
// toggle is named for what it does rather than carrying the value a second time: a screen
// reader that announced the value as content AND again as a button name would read a 64
// character fingerprint twice on every row. The affordance exists for the sighted operator,
// who is the only person a CSS clip actually blocks.
//
// important: handing this component a pre-sliced string puts the defect back, one level down,
// where no guard is looking. Every call site that used to slice deletes the slice rather than
// passing its result through.
//
// important: the control is a real <button>, in both modes. Not a <span> with an onClick and
// not a bare span inside a TooltipTrigger. Two live screens did exactly that, and a span given
// trigger props takes no focus, gets no focus ring and ignores Enter, so the reveal existed for
// a mouse and for nobody else.
//
// note: no motion. Two icons rather than one rotating icon, so there is no transform to guard
// and no new keyframe to wrap in a prefers-reduced-motion branch. A disclosure that snaps is
// honest, and it is what the overflow-cell dialog already does.
export interface OverflowTextProps {
  /**
   * The whole value. Never pre-sliced and never pre-ellipsised.
   * `value.slice(0, 12)` passed here is a bug, not a shorter label.
   */
  value: string;
  /**
   * What the value is, in the operator's words, ex; "fingerprint for ubuntu-24.04".
   * Names both buttons.
   *
   * important: required, for the same reason it is required on CopyableText. An icon-only
   * button renders as a bare "button" in the accessibility tree unless the element itself names
   * it, a wrapping Tooltip contributes a description rather than a name, and twenty unnamed
   * toggles down one column announce identically. check-icon-button-names.mjs enforces it.
   */
  label: string;
  /**
   * "inline" expands the value in place. Use it anywhere the container can grow taller without
   * displacing something the operator is watching.
   *
   * "overlay" puts the value in a Popover, for chrome that is size locked: a console info panel
   * pinned over a live terminal, a fixed height title bar.
   *
   * note: a Popover and not a Tooltip. Radix tooltips open on hover and on focus and do not
   * open on touch, they contribute aria-describedby rather than a name, and their content
   * cannot hold a real copy button.
   *
   * important: neither mode is correct when the clipped text already sits INSIDE a control. A
   * button nested in a button is invalid markup, the two compete for the click, and inside a
   * Radix menu the inner one is unreachable because roving focus only reaches menu items. For a
   * SelectTrigger, a FilterTrigger, a tray button or a DropdownMenuItem, name the existing
   * control with the whole value and make the surface it opens print the value in full.
   */
  mode?: "inline" | "overlay";
  /**
   * Lines kept at rest. 1 clips an identifier to a single line. 2 and 3 clamp prose such as a
   * warning message or a project description. "none" never clips: the value always wraps, for a
   * surface with room to show it, which is what a detail dialog is.
   *
   * note: 1 uses `truncate`, not `line-clamp-1`. They differ. `truncate` refuses to wrap at
   * all, which is what a value in a dense grid wants; `line-clamp-1` lets the text wrap and then
   * hides everything below the first line.
   */
  lines?: 1 | 2 | 3 | "none";
  /** Monospace at rest and revealed. `font-mono` on the span, never a <code> element. */
  mono?: boolean;
  /**
   * Characters kept visible at rest on one line, ex; 12 for a certificate fingerprint, which is
   * exactly what `lxc config trust list` prints, so the two can be matched by eye.
   *
   * important: a CSS width, not a cut. The span is capped at exactly this many `ch` and the whole
   * value stays in the DOM, which is the difference from the slice this component replaced. The
   * clip carries no ellipsis: the trust list prints its 12 characters bare, the reveal control
   * beside the value already says there is more, and the ellipsis glyph is wider than one cell in
   * the house mono face, so it would eat the twelfth character. Only meaningful with `mono` and
   * `lines={1}`: `ch` is a glyph width only in a monospace face.
   */
  visibleChars?: number;
  /**
   * When the copy button appears. "expanded" costs nothing at rest, which is what keeps a table
   * at its current rows per screen. "always" is the detail-field preset, where copying the value
   * is the reason the field is on screen.
   */
  copy?: "expanded" | "always" | "none";
  /**
   * "auto" measures and drops the control when the value genuinely fits. "always" skips
   * measurement for a value that is opaque and fixed length, ex; a 64 character fingerprint or a
   * UUID, which never fits and so is work with a known answer.
   *
   * note: "auto" is safe by construction because it starts pessimistic, so "always" is an
   * optimisation and never a correctness lever. Reaching for it to fix a missing control means
   * the measurement is wrong, and the measurement is the bug.
   */
  reveal?: "auto" | "always";
  /**
   * Which surface the control is measured against. "surface" is the themed app and reads
   * --muted-foreground. "chrome" is the console and terminal shell, which paints its own fixed
   * dark background and does not follow the theme, so a token tuned against --card is the wrong
   * measurement there. check-semantic-colour.mjs exempts that tree by path for the same reason.
   */
  tone?: "surface" | "chrome";
  /** Reports the copy result. The container owns the live region, not this component. */
  onCopied?: (ok: boolean) => void;
  className?: string;
  /**
   * A decorated rendering of the SAME value, ex; a highlighted search match, or a TableLink that
   * drills into the record. Pass a function to receive the expanded state, which a decorated
   * rendering needs when it does its own clipping: TableLink wraps its text in a `truncate` span,
   * so it has to be told to stop, or the toggle beside it would expand a box whose contents are
   * still clipped one element further in.
   *
   * important: it must render `value` in full. A shortened form here reintroduces the defect one
   * level down, where no guard is looking.
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
  // never the other way round, so a browser with no ResizeObserver and a first render before
  // hydration both keep the reveal rather than losing it. It also makes the server HTML and the
  // first client render identical, so there is nothing for hydration to mismatch on.
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
  // note: only where something is actually clipped. lines="none" is the detail-surface preset,
  // which is not a table row and has room, and pulling four pixels off the top and bottom there
  // tightened the gap between a dialog's header and its first section. A negative margin applied
  // where it is not earning anything is just a layout bug waiting to be seen.
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
    // Nothing is clipped, so there is nothing behind this and it must not pretend otherwise.
    // A Popover whose trigger never renders is also a Radix root with no anchor.
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
          // Radix-injected aria-controls is how filter-trigger.tsx ended up pointing at an id no
          // element carried.
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
