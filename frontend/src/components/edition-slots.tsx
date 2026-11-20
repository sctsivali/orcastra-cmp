"use client";

// Places in the shared layout where an edition renders its own pieces (a search box in the
// header, notices and global dialogs beside the layout's). Full renders nothing in either.

import { MiniAccessGateExtras, MiniLayoutExtras } from "@/editions/mini/slots";
import { MiniHeaderSearch } from "@/editions/mini/search/command-palette";

export function EditionHeaderStart(): JSX.Element | null {
  return <MiniHeaderSearch />;
}

export function EditionLayoutExtras(): JSX.Element | null {
  return <MiniLayoutExtras />;
}

// What an edition adds to the access gate's card (the gate replaces the whole shell).
export function EditionAccessGateExtras(): JSX.Element | null {
  return <MiniAccessGateExtras />;
}

// Pages an edition keeps open to a signed-in account the access gate stops: they render on their
// own, without the shell, whose reads the backend would refuse. Mini: an operator whose project
// access ended can still see and renew the certificate it signs in with.
export const EDITION_GATE_EXEMPT_PATHS: readonly string[] = ["/my-certificate"];

// An edition whose sign-in is not an identity-provider hand-off supplies its own sign-in page body.
export { MiniSignIn as EditionSignIn } from "@/editions/mini/sign-in";

// An edition whose accounts do not live on an identity provider supplies its own Profile entry for
// the user menu (in place of the link to the provider's account page).
export { MiniProfileItem as EditionProfileItem } from "@/editions/mini/slots";
