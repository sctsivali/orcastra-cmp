"use client";

import { useEffect } from "react";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ChevronRight, Activity, Settings2, Palette, Shield, Users } from "lucide-react";
import { DesktopControlSettings } from "@/components/desktop-control-settings";
import { SystemResourceUsagePanel } from "@/components/system-resource-usage";
import { OrgPersonalizationSettings } from "@/components/org-personalization-settings";
import { PersonalizationSettings } from "@/components/personalization-settings";
import { IntegrationsSettings } from "@/components/integrations-settings";
import { SyncedOrganizationsPanel } from "@/components/integrations/synced-organizations-panel";
import { UserGroupsSettings } from "@/components/user-groups-settings";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { LoadingLottie } from "@/components/loading-lottie";
import { CacheSettings } from "./components/cache-settings";
import { useSettingsNotice } from "./components/settings-notice";
import { settingsTabPath, type SettingsTab } from "./settings-tabs";

export function SettingsView({ tab }: { tab: SettingsTab }) {
  const { navigate } = useNavigationProgress();
  const { showRestrictedNotice, dismissNotice } = useSettingsNotice();
  const { isAdmin, isPartner, roles, isLoading } = usePermissions();
  const managesOrganizationBranding = roles.includes(Role.PARTNER);
  const denied = !isLoading && tab !== "general" && !isPartner;

  useEffect(() => {
    if (!denied) return;
    showRestrictedNotice();
    navigate("/settings", { replace: true });
  }, [denied, navigate, showRestrictedNotice]);

  // important: a forbidden deep link never mounts a privileged panel or starts its reads.
  if (isLoading || denied) {
    return <div className="flex min-h-[40vh] items-center justify-center" role="status" aria-label="Loading settings"><LoadingLottie /></div>;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Settings</h1>
        <p className="text-muted-foreground mt-2">
          {isPartner
            ? "Configure system preferences, manage integrations, user access, personalization, and monitor resource usage"
            : "Configure your preferences and manage application cache"}
        </p>
      </div>

      <Tabs value={tab} onValueChange={(value) => {
        dismissNotice();
        navigate(settingsTabPath(value as SettingsTab));
      }} className="space-y-6">
        <div className="max-w-full overflow-x-auto">
        <TabsList className="inline-flex w-auto h-auto p-1">
          <TabsTrigger value="general" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Settings2 className="h-4 w-4 shrink-0" />
            <span>General</span>
          </TabsTrigger>
          {/* Integrations - Admin + Partner */}
          {isPartner && (
          <TabsTrigger value="integrations" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Shield className="h-4 w-4 shrink-0" />
            <span>Integrations</span>
          </TabsTrigger>
          )}
          {/* User Groups - Admin + Partner (partners can assign tenants) */}
          {isPartner && (
          <TabsTrigger value="user-groups" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Users className="h-4 w-4 shrink-0" />
            <span>User Groups</span>
          </TabsTrigger>
          )}

          {/* Personalization - Admin + Partner */}
          {isPartner && (
          <TabsTrigger value="personalization" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Palette className="h-4 w-4 shrink-0" />
            <span>Personalization</span>
          </TabsTrigger>
          )}
          {/* System Info - Admin + Partner */}
          {isPartner && (
          <TabsTrigger value="dashboard-host" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Activity className="h-4 w-4 shrink-0" />
            <span>Dashboard Host</span>
          </TabsTrigger>
          )}
        </TabsList>
        </div>

        {/* General Settings Tab - all roles, My Keys hidden for Tenant */}
        <TabsContent value="general" className="space-y-6">
          <div className="grid gap-6">
            {/* Desktop Control lives in Integrations, beside the API keys it governs. A tenant
                cannot open that tab, and this consent is deliberately theirs to give rather than
                their partner's, so for them it stays here. */}
            {!isPartner && <DesktopControlSettings />}
            {/* Extensions pointer - the surface is a route, not a tab, because a refresh
                inside a tab would destroy the one-time credential an install shows. */}
            {isPartner && (
            <Card className="hover:shadow-lg transition-shadow cursor-pointer" onClick={() => navigate('/extensions')}>
              <CardHeader>
                <CardTitle>Extensions</CardTitle>
                <CardDescription>
                  Install add-ons built against the Orcastra API
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <p className="text-sm text-muted-foreground">
                    An add-on is an external application that runs on its author&apos;s own
                    infrastructure and reaches Orcastra with a credential scoped to one
                    organization. No add-on code runs inside this dashboard.
                  </p>
                  <Button
                    variant="default"
                    className="gap-2 mt-2"
                    onClick={(e) => {
                      e.stopPropagation();
                      navigate('/extensions');
                    }}
                  >
                    Manage Extensions
                  </Button>
                </div>
              </CardContent>
            </Card>
            )}

            {/* My Keys Card - Admin + Partner only */}
            {isPartner && (
            <Card className="hover:shadow-lg transition-shadow cursor-pointer" onClick={() => navigate('/my-keys')}>
              <CardHeader>
                <CardTitle>
                  My Keys
                </CardTitle>
                <CardDescription>
                  Manage TLS identity keys for accessing hypervisor resources
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <p className="text-sm text-muted-foreground">
                    Generate and manage TLS certificates for secure access to your clusters and projects.
                    Generate keys with custom permissions and expiration dates.
                  </p>
                  <Button
                    variant="default"
                    className="gap-2 mt-2"
                    onClick={(e) => {
                      e.stopPropagation();
                      navigate('/my-keys');
                    }}
                  >
                    Manage Keys
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </div>
              </CardContent>
            </Card>
            )}

            <CacheSettings isAdmin={isAdmin} />
          </div>
        </TabsContent>

        {/* System Info Tab - Admin + Partner */}
        {isPartner && (
        <TabsContent value="dashboard-host">
          <SystemResourceUsagePanel />
        </TabsContent>
        )}

        {/* Personalization Tab - Partner role edits org branding, admin-only edits personal prefs */}
        {isPartner && (
        <TabsContent value="personalization">
          {managesOrganizationBranding ? (
            <OrgPersonalizationSettings />
          ) : (
            <PersonalizationSettings />
          )}
        </TabsContent>
        )}

        {/* Integrations Tab - Admin + Partner */}
        {isPartner && (
        <TabsContent value="integrations" className="space-y-6">
          <IntegrationsSettings />
          {/* Which key owns each synced organization. Only the owner syncs it, so an owner that
              stopped working freezes it, and this is where an administrator moves it. */}
          <SyncedOrganizationsPanel />
          {/* The consent that decides whether the desktop and guest-access capabilities on those
              keys do anything. It stays a card of its own rather than a switch inside the create
              dialog: it is one standing decision per organization, read on every call and shared
              with every organization on the cluster, not a property of the key being made. */}
          <DesktopControlSettings />
        </TabsContent>
        )}

        {/* User Groups Tab - Admin + Partner */}
        {isPartner && (
        <TabsContent value="user-groups">
          <UserGroupsSettings />
        </TabsContent>
        )}

      </Tabs>
    </div>
  );
}
