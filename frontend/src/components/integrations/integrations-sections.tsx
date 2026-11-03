"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Building2, KeyRound, Monitor } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  SectionPanel, SectionSwitcher, useSectionChangeEffects, useVisitedSections,
  type SectionSwitcherItem,
} from "@/components/ui/section-switcher";
import { IntegrationsSettings } from "@/components/integrations-settings";
import { SyncedOrganizationsPanel } from "@/components/integrations/synced-organizations-panel";
import { DesktopControlSettings } from "@/components/desktop-control-settings";
import { usePermissions } from "@/hooks/use-permissions";
import { useSettingsSection } from "@/hooks/use-settings-section";
import { api } from "@/lib/api";
import { organizationIsFrozen, openSyncedOrganizations } from "@/lib/key-ownership-display";
import { retryUnlessUnavailable } from "@/lib/read-error";
import { plural } from "@/lib/utils";
import {
  INTEGRATIONS_SECTIONS, RevealIntegrationsSectionContext, type IntegrationsSection,
} from "./integrations-section-context";

/**
 * Settings > Integrations: API keys, the organizations they sync, and the desktop consent.
 *
 * important: three sections behind a switcher, not three stacked panels. Stacked, the key grid
 * (twelve tall cards a page) pushed Synced Organizations and Desktop Control about 2000px below
 * the fold, and with them the warning that an organization's owner key had stopped working.
 */
function IntegrationsSectionsContent() {
  const { isAdmin } = usePermissions();
  const { section, select, hrefFor } = useSettingsSection(INTEGRATIONS_SECTIONS, "keys");
  const visited = useVisitedSections(section);
  const navRef = useRef<HTMLElement>(null);
  useSectionChangeEffects(section, navRef);

  // The same keys and options the sections read, so these are shared reads, not extra ones.
  const keysQuery = useQuery({
    queryKey: ["integrations", "api-keys"],
    queryFn: () => api.integrations.listAPIKeys(),
    staleTime: 30000,
    retry: retryUnlessUnavailable,
  });
  const orgsQuery = useQuery({
    queryKey: ["integrations", "organizations"],
    queryFn: () => api.integrations.listOrganizations(),
    staleTime: 30000,
    retry: retryUnlessUnavailable,
  });
  // Only an administrator is told an owner key's state; a partner reads null and counts nothing.
  const frozen = isAdmin ? (orgsQuery.data ?? []).filter(organizationIsFrozen).length : 0;

  const [focusPending, setFocusPending] = useState<IntegrationsSection | null>(null);
  const reveal = useCallback(
    (next: IntegrationsSection) => {
      select(next);
      setFocusPending(next);
    },
    [select],
  );
  useEffect(() => {
    if (focusPending !== section) return;
    setFocusPending(null);
    // after the commit that mounted the section, so the element exists to scroll to and focus
    if (section === "organizations") requestAnimationFrame(() => openSyncedOrganizations());
  }, [focusPending, section]);

  const items: SectionSwitcherItem<IntegrationsSection>[] = [
    { id: "keys", label: "API Keys", icon: KeyRound, count: keysQuery.data?.total },
    {
      id: "organizations",
      label: "Synced Organizations",
      icon: Building2,
      count: orgsQuery.data?.length,
      attention: frozen > 0
        ? { count: frozen, label: frozen === 1 ? "needs a new owner key" : "need a new owner key" }
        : undefined,
    },
    { id: "desktop-control", label: "Desktop Control", icon: Monitor },
  ];

  return (
    <RevealIntegrationsSectionContext.Provider value={reveal}>
      <div className="space-y-6">
        <div>
          <h2 className="text-2xl font-bold">Integrations</h2>
          <p className="text-sm text-muted-foreground mt-1">
            API keys for external applications, the organizations they sync, and what an agent may
            do on their desktops.
          </p>
        </div>

        <SectionSwitcher
          label="Integrations sections"
          items={items}
          active={section}
          hrefFor={hrefFor}
          onSelect={select}
          navRef={navRef}
        />

        {/* The warning the stacked layout buried, on every section but its own. */}
        {frozen > 0 && section !== "organizations" && (
          <Alert>
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
              <span>
                {plural(frozen, "organization has", "organizations have")} an owner key that no longer
                works. Their sync and provisioning are paused until you transfer them.
              </span>
              <Button size="sm" variant="outline" onClick={() => reveal("organizations")}>
                Review
              </Button>
            </AlertDescription>
          </Alert>
        )}

        <SectionPanel id="keys" label="API Keys" active={section === "keys"} visited={visited.has("keys")}>
          <IntegrationsSettings />
        </SectionPanel>
        <SectionPanel
          id="organizations"
          label="Synced Organizations"
          active={section === "organizations"}
          visited={visited.has("organizations")}
        >
          {/* Which key owns each synced organization. Only the owner syncs it, so an owner that
              stopped working freezes it, and this is where an administrator moves it. */}
          <SyncedOrganizationsPanel />
        </SectionPanel>
        <SectionPanel
          id="desktop-control"
          label="Desktop Control"
          active={section === "desktop-control"}
          visited={visited.has("desktop-control")}
        >
          {/* The consent that decides whether the desktop and guest-access capabilities on those
              keys do anything. One standing decision per organization, read on every call and
              shared with every organization on the cluster, not a property of the key being made. */}
          <DesktopControlSettings />
        </SectionPanel>
      </div>
    </RevealIntegrationsSectionContext.Provider>
  );
}

/** The boundary `useSearchParams` requires, so the tab can mount this anywhere. */
export function IntegrationsSections() {
  return (
    <Suspense fallback={<Skeleton className="h-48 rounded-lg" />}>
      <IntegrationsSectionsContent />
    </Suspense>
  );
}
