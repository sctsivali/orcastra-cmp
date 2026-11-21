"use client";

// Places in the shared layout where an edition renders its own pieces (a search box in the
// header, notices and global dialogs beside the layout's). Full renders nothing in either.

export function EditionHeaderStart(): JSX.Element | null {
  return null;
}

export function EditionLayoutExtras(): JSX.Element | null {
  return null;
}

// What an edition adds to the access gate's card (the gate replaces the whole shell).
export function EditionAccessGateExtras(): JSX.Element | null {
  return null;
}

// Pages an edition keeps open to a signed-in account the access gate stops. They render on their
// own, without the shell, whose reads the backend would refuse. Full has none.
export const EDITION_GATE_EXEMPT_PATHS: readonly string[] = [];

// An edition whose sign-in is not an identity-provider hand-off supplies its own sign-in page body.
export const EditionSignIn: (() => JSX.Element) | null = null;

// An edition whose accounts do not live on an identity provider supplies its own Profile entry for
// the user menu (in place of the link to the provider's account page).
export const EditionProfileItem: (() => JSX.Element) | null = null;
