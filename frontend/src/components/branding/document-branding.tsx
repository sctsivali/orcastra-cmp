"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import { usePersonalization } from "@/components/personalization-provider";
import { DEFAULT_PERSONALIZATION } from "@/types/personalization";

/**
 * The tab title and icon for a signed-in account: its organization's branding as resolved on the
 * server. Before sign-in the root layout's metadata already shows the deployment default; this
 * takes over once the account's own branding is known, and puts the server's values back when
 * the account has none.
 */
export function DocumentBranding() {
  const { settings, isLoading } = usePersonalization();
  const pathname = usePathname();
  const serverTitleRef = React.useRef<string | null>(null);
  const appliedTitleRef = React.useRef<string | null>(null);
  const appName = settings.branding.appName;
  const favicon = settings.logo.favicon;

  React.useEffect(() => {
    if (isLoading) return;
    // note: Next may render the root metadata again on a navigation, replacing the title and the
    // icon links with the server's. Whatever is there and was not written here is the server's.
    if (document.title !== appliedTitleRef.current) serverTitleRef.current = document.title;
    const branded = appName && appName !== DEFAULT_PERSONALIZATION.branding.appName;
    const title = branded ? appName : serverTitleRef.current ?? document.title;
    document.title = title;
    appliedTitleRef.current = title;
    for (const link of Array.from(document.querySelectorAll<HTMLLinkElement>('link[rel~="icon"]'))) {
      if (link.dataset.serverHref === undefined) link.dataset.serverHref = link.href;
      link.href = favicon || link.dataset.serverHref;
    }
  }, [appName, favicon, isLoading, pathname]);

  return null;
}
