"use client";

import * as React from "react";
import { ExternalLink } from "lucide-react";
import { IntentLink } from "@/components/navigation/route-intent";
import { cn } from "@/lib/utils";

// Clickable text inside a table cell.
//
// The affordance shows at rest, not only on hover, so an operator does not have to sweep the
// pointer across dense cells to find what is clickable. Dotted follows the platform's "more
// behind this" convention: the Tailwind preflight applies `underline dotted` to abbr[title].
//
// Always a real <button> or <a>, so Tab reaches it and Enter or Space activates it. A
// <TableCell onClick> is mouse-only: a td is not focusable and gets no focus ring.
//
// Hover does not switch to text-primary. globals.css binds --primary to the user-configurable
// personalization accent, which at the default is about 4.06:1 against --card in light mode,
// below AA for 14px text. The decoration carries the state change so the text keeps full
// contrast.
const tableLinkClass =
  "inline-flex max-w-full items-center gap-1 rounded-sm text-left align-baseline " +
  "underline decoration-dotted decoration-1 underline-offset-4 decoration-muted-foreground " +
  "hover:decoration-solid hover:decoration-foreground " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring " +
  "focus-visible:ring-offset-1 focus-visible:ring-offset-background " +
  "transition-colors motion-reduce:transition-none " +
  "disabled:pointer-events-none disabled:opacity-60";

type CommonProps = {
  children: React.ReactNode;
  className?: string;
  title?: string;
  "aria-label"?: string;
  /**
   * Stop clipping the text and let it wrap instead.
   *
   * The clip stays the default because it stops a 64 character fingerprint from setting the
   * width of a whole column. A caller that owns a reveal control passes the revealed state
   * through here, so the revealed value is not still clipped inside the link.
   *
   * `true` breaks anywhere, for one long token such as a fingerprint. `"words"` breaks only at
   * spaces, for a short list whose items must stay whole, such as addresses.
   */
  wrap?: boolean | "words";
};

type ActionProps = CommonProps & {
  onClick: (event: React.MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  href?: never;
  external?: never;
};

type NavProps = CommonProps & {
  href: string;
  external?: boolean;
  onClick?: never;
  disabled?: never;
};

// Discriminated union: passing both href and onClick is a compile error.
export type TableLinkProps = ActionProps | NavProps;

export function TableLink(props: TableLinkProps) {
  const { children, className, title, wrap } = props;
  const ariaLabel = props["aria-label"];
  const classes = cn(tableLinkClass, className);
  const textClass = cn(
    "min-w-0",
    wrap === "words" ? "whitespace-normal text-left" : wrap ? "whitespace-normal break-all text-left" : "truncate",
  );

  if ("href" in props && props.href !== undefined) {
    if (props.external) {
      return (
        <a
          href={props.href}
          target="_blank"
          rel="noopener noreferrer"
          title={title}
          aria-label={ariaLabel}
          className={classes}
        >
          <span className={textClass}>{children}</span>
          <ExternalLink className="h-3 w-3 shrink-0" aria-hidden="true" />
        </a>
      );
    }
    // IntentLink rather than a plain Link: on the default prefetch every visible row would fire
    // its own RSC request. IntentLink warms on hover, one request for the row being read.
    return (
      <IntentLink href={props.href} title={title} aria-label={ariaLabel} className={classes}>
        <span className={textClass}>{children}</span>
      </IntentLink>
    );
  }

  return (
    <button
      type="button"
      onClick={props.onClick}
      disabled={props.disabled}
      title={title}
      aria-label={ariaLabel}
      className={classes}
    >
      <span className={textClass}>{children}</span>
    </button>
  );
}
