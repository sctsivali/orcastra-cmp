"use client";

import { Suspense, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, Users, Wifi, WifiOff } from "lucide-react";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  SectionPanel, SectionSwitcher, useSectionChangeEffects, useVisitedSections,
  type SectionSwitcherItem,
} from "@/components/ui/section-switcher";
import { useSettingsSection } from "@/hooks/use-settings-section";
import { api } from "@/lib/api";
import { PolicyManagementSection } from "@/components/policy-management-section";
import { AssignedUsersPanel } from "@/components/user-groups/assigned-users-panel";
import type { AuthentikSyncStatus } from "@/types";

const SECTIONS = ["users", "policies"] as const;
type UserGroupsSection = (typeof SECTIONS)[number];

/**
 * Whether role changes reach the identity provider, in one line under the heading.
 *
 * note: nothing is claimed before the status has been read. A missing answer used to read as
 * "Not configured", and an unreachable one as "saved locally only", which a partner's assign
 * contradicts: it is refused (503) until the identity provider answers.
 */
function IdentityProviderStatus({ status, isError }: { status?: AuthentikSyncStatus; isError: boolean }) {
  let icon = <WifiOff className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />;
  let text = "Checking the identity provider…";
  if (status) {
    if (!status.authentik_configured) {
      text = "Identity provider sync is not configured. Set its credentials to sync role changes.";
    } else if (!status.authentik_reachable) {
      icon = <WifiOff className="h-4 w-4 shrink-0 text-status-pending" aria-hidden="true" />;
      text = `${status.details || "Identity provider unreachable"}. Role changes cannot be confirmed until it answers.`;
    } else {
      icon = <Wifi className="h-4 w-4 shrink-0 text-status-online" aria-hidden="true" />;
      text = "Identity provider connected. Role changes sync automatically.";
    }
  } else if (isError) {
    text = "Could not check the identity provider.";
  }
  return (
    <p className="mt-2 flex items-center gap-2 text-sm text-muted-foreground" role="status">
      {icon}
      <span>{text}</span>
    </p>
  );
}

/**
 * Settings > User Groups: who holds which role where, and the access policies tenants get.
 *
 * important: two sections behind a switcher. Stacked, Assigned Users sat under the identity
 * provider card and the policy table, and the list an operator came for was the last thing on
 * the page.
 */
function UserGroupsSettingsContent() {
  const queryClient = useQueryClient();
  const { section, select, hrefFor } = useSettingsSection(SECTIONS, "users");
  const visited = useVisitedSections(section);
  const navRef = useRef<HTMLElement>(null);
  useSectionChangeEffects(section, navRef);

  // The same reads the two sections make, shared, for the counts on the switcher.
  const usersQuery = useQuery({
    queryKey: ["user-groups"],
    queryFn: () => api.userGroups.list(),
  });
  const policiesQuery = useQuery({
    queryKey: ["tenant-access-policies"],
    queryFn: () => api.tenantAccess.listPolicies(),
  });
  const syncQuery = useQuery({
    queryKey: ["user-groups-sync-status"],
    queryFn: () => api.userGroups.syncStatus(),
    refetchInterval: 30000,
  });

  // note: failures propagate to the refresh control, which reports them. Success
  // is reported inline by the same control rather than by a toast.
  async function handleRefreshAll() {
    await Promise.all([
      syncQuery.refetch({ throwOnError: true }),
      usersQuery.refetch({ throwOnError: true }),
      queryClient.refetchQueries({ queryKey: ["tenant-access-policies"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["integration-organizations"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["organizations"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["clusters"], type: "active" }),
    ]);
  }

  const items: SectionSwitcherItem<UserGroupsSection>[] = [
    { id: "users", label: "Assigned Users", icon: Users, count: usersQuery.data?.length },
    { id: "policies", label: "Access Policies", icon: ShieldCheck, count: policiesQuery.data?.policies?.length },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-2xl font-bold">User Groups</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Manage user roles, organizations, and the access policies tenants receive.
          </p>
          <IdentityProviderStatus status={syncQuery.data} isError={syncQuery.isError} />
        </div>
        <RefreshButton
          label="Refresh user groups, policies, and sync status"
          lastUpdatedAt={usersQuery.dataUpdatedAt}
          onRefresh={handleRefreshAll}
        />
      </div>

      <SectionSwitcher
        label="User Groups sections"
        items={items}
        active={section}
        hrefFor={hrefFor}
        onSelect={select}
        navRef={navRef}
      />

      <SectionPanel id="users" label="Assigned Users" active={section === "users"} visited={visited.has("users")}>
        <AssignedUsersPanel />
      </SectionPanel>
      <SectionPanel id="policies" label="Access Policies" active={section === "policies"} visited={visited.has("policies")}>
        <PolicyManagementSection />
      </SectionPanel>
    </div>
  );
}

/** The boundary `useSearchParams` requires, so the tab can mount this anywhere. */
export function UserGroupsSettings() {
  return (
    <Suspense fallback={<Skeleton className="h-64 rounded-xl" />}>
      <UserGroupsSettingsContent />
    </Suspense>
  );
}
