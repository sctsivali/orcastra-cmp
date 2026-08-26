"use client";

import * as React from "react";
import { ExternalLink } from "lucide-react";
import { IntentLink } from "@/components/navigation/route-intent";
import { cn } from "@/lib/utils";

// Clickable text inside a table cell.
//
// important: the affordance is present at rest, not on hover. Table cells are dense, and a
// hover-only hint forces the operator to sweep the pointer across every cell to discover
// what is clickable. Dotted is the platform's own "there is more behind this" convention:
// the Tailwind preflight applies `underline dotted` to abbr[title]. It reads differently
// from a spellcheck squiggle, which is wavy, red, and tight to the baseline.
//
// important: always a real <button> or <a>, so Tab reaches it and Enter or Space activates
// it. The pattern this replaces was <TableCell onClick>, which is mouse-only: a td is not
// focusable, gets no focus ring, and is invisible to keyboard and switch users.
//
// note: hover deliberately does not go to text-primary. globals.css rebinds --primary to
// the user-configurable personalization accent, which at the shipped default measures
// about 4.06:1 against --card in light mode, below AA for 14px text, with no clamp on the
// picker. The decoration carries the state change instead, so the text stays at full
// contrast in every state.
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
   * important: the clip below is not decoration, it is what stops a 64 character fingerprint
   * from setting the width of a whole column, so it stays the default. But it also means the
   * link is a second, invisible clip inside anything that reveals a long value, and a reveal
   * whose own content is still truncated is the defect this codebase has shipped more than once.
   * A caller that owns a reveal control passes the revealed state straight through here.
   */
  wrap?: boolean;
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

// Discriminated union: passing both href and onClick is a compile error, which is most of
// the risk when converting ~14 hand-rolled call sites.
export type TableLinkProps = ActionProps | NavProps;

export function TableLink(props: TableLinkProps) {
  const { children, className, title, wrap } = props;
  const ariaLabel = props["aria-label"];
  const classes = cn(tableLinkClass, className);
  const textClass = cn("min-w-0", wrap ? "whitespace-normal break-all text-left" : "truncate");

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
    // important: IntentLink, not a plain Link. A table row is rendered once per record, so a
    // link on the default prefetch fires one full RSC request per visible row: the networks
    // table alone issued 26. Warming on hover instead costs one request for the row the
    // operator is actually reading.
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
