"use client";

import { Suspense } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import { WifiOff, Wifi } from "lucide-react";
import { api } from "@/lib/api";
import { PolicyManagementSection } from "@/components/policy-management-section";
import { AssignedUsersPanel } from "@/components/user-groups/assigned-users-panel";

function UserGroupsSettingsContent() {
  const queryClient = useQueryClient();

  const {
    dataUpdatedAt: usersUpdatedAt,
  } = useQuery({
    queryKey: ["user-groups"],
    queryFn: () => api.userGroups.list(),
  });

  // identity provider sync status
  const { data: syncStatus, refetch: refetchSync } = useQuery({
    queryKey: ["user-groups-sync-status"],
    queryFn: () => api.userGroups.syncStatus(),
    refetchInterval: 30000, // check every 30s
  });

  // note: failures propagate to the refresh control, which reports them. Success
  // is reported inline by the same control rather than by a toast.
  async function handleRefreshAll() {
    await Promise.all([
      refetchSync({ throwOnError: true }),
      queryClient.refetchQueries({ queryKey: ["user-groups"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["tenant-access-policies"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["integration-organizations"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["organizations"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["clusters"], type: "active" }),
    ]);
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-2xl font-bold">User Groups</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Manage user roles, organizations, access policies, and identity provider sync.
          </p>
        </div>
        <RefreshButton
          label="Refresh user groups, policies, and sync status"
          lastUpdatedAt={usersUpdatedAt}
          onRefresh={handleRefreshAll}
        />
      </div>

      {/* Identity Provider Sync Status */}
      <Card>
        <CardHeader className="pb-3">
          <div>
            <CardTitle className="text-base flex items-center gap-2">
              Identity Provider Sync Status
            </CardTitle>
            <CardDescription className="mt-1 text-xs">
              User roles and group memberships are synchronized from your identity provider (Authentik).
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-3">
            {syncStatus?.authentik_configured ? (
              syncStatus.authentik_reachable ? (
                <>
                  <Wifi className="h-4 w-4 text-status-online" aria-hidden="true" />
                  <span className="text-sm">
                    {syncStatus.groups_exist
                      ? "Connected. Role changes and groups sync automatically."
                      : "Connected. Role changes sync automatically."}
                  </span>
                </>
              ) : (
                <>
                  <WifiOff className="h-4 w-4 text-status-pending" aria-hidden="true" />
                  <span className="text-sm">
                    Configured but unreachable. Changes are saved locally only.
                  </span>
                </>
              )
            ) : (
              <>
                <WifiOff className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">
                  Not configured. Set identity provider credentials to enable sync.
                </span>
              </>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Access Policies Management */}
      <PolicyManagementSection />

      <AssignedUsersPanel />
    </div>
  );
}

/** Carries its own boundary so the tab can drop it anywhere without knowing what reads the URL. */
export function UserGroupsSettings() {
  return (
    <Suspense fallback={<Skeleton className="h-64 rounded-xl" />}>
      <UserGroupsSettingsContent />
    </Suspense>
  );
}
