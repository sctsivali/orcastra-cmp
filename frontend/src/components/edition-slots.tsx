"use client";

// Places in the shared layout where an edition renders its own pieces (a search box in the
// header, notices and global dialogs beside the layout's). Full renders nothing in either.

import { MiniLayoutExtras } from "@/editions/mini/slots";

export function EditionHeaderStart(): JSX.Element | null {
  return null;
}

export function EditionLayoutExtras(): JSX.Element | null {
  return <MiniLayoutExtras />;
}

// An edition whose sign-in is not an identity-provider hand-off supplies its own sign-in page body.
export { MiniSignIn as EditionSignIn } from "@/editions/mini/sign-in";

// An edition whose accounts do not live on an identity provider supplies its own Profile entry for
// the user menu (in place of the link to the provider's account page).
export { MiniProfileItem as EditionProfileItem } from "@/editions/mini/slots";
