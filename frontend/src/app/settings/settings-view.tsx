"use client";

import { useEffect } from "react";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ExtensionsIllustration, KeyVaultIllustration } from "@/components/illustrations/scenes/catalog";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ChevronRight, Activity, Settings2, Palette, Shield, Users } from "lucide-react";
import { DesktopControlSettings } from "@/components/desktop-control-settings";
import { SystemResourceUsagePanel } from "@/components/system-resource-usage";
import { OrgPersonalizationSettings } from "@/components/org-personalization-settings";
import { ReleaseStatusCard } from "@/components/release/release-status-card";
import { IntegrationsSections } from "@/components/integrations/integrations-sections";
import { UserGroupsSettings } from "@/components/user-groups-settings";
import { usePermissions } from "@/hooks/use-permissions";
import { LoadingLottie } from "@/components/loading-lottie";
import { CacheSettings } from "./components/cache-settings";
import { useSettingsNotice } from "./components/settings-notice";
import { settingsTabPath, type SettingsTab } from "./settings-tabs";

export function SettingsView({ tab }: { tab: SettingsTab }) {
  const { navigate } = useNavigationProgress();
  const { showRestrictedNotice, dismissNotice } = useSettingsNotice();
  const { isAdmin, isPartner, isLoading } = usePermissions();
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
          {/* isPartner is true for Administrators as well. */}
          {isPartner && (
          <TabsTrigger value="integrations" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Shield className="h-4 w-4 shrink-0" />
            <span>Integrations</span>
          </TabsTrigger>
          )}
          {isPartner && (
          <TabsTrigger value="user-groups" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Users className="h-4 w-4 shrink-0" />
            <span>User Groups</span>
          </TabsTrigger>
          )}

          {isPartner && (
          <TabsTrigger value="personalization" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Palette className="h-4 w-4 shrink-0" />
            <span>Personalization</span>
          </TabsTrigger>
          )}
          {isPartner && (
          <TabsTrigger value="dashboard-host" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Activity className="h-4 w-4 shrink-0" />
            <span>Dashboard Host</span>
          </TabsTrigger>
          )}
        </TabsList>
        </div>

        <TabsContent value="general" className="space-y-6">
          <div className="grid gap-6">
            {/* Desktop Control lives in Integrations, beside the API keys it governs. A tenant
                cannot open that tab, and this consent is the tenant's own to give, so for a
                tenant it stays here. */}
            {!isPartner && <DesktopControlSettings />}
            {/* Extensions is a route rather than a tab: a refresh inside a tab would destroy
                the one-time credential an install shows. */}
            {isPartner && (
            <Card className="illo-launch illo-host hover:shadow-lg transition-shadow cursor-pointer" onClick={() => navigate('/extensions')}>
              <div className="flex items-center">
                <div className="min-w-0 flex-1">
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
                </div>
                <ExtensionsIllustration size="lg" className="mr-6" />
              </div>
            </Card>
            )}

            {isPartner && (
            <Card className="illo-launch illo-host hover:shadow-lg transition-shadow cursor-pointer" onClick={() => navigate('/my-keys')}>
              <div className="flex items-center">
                <div className="min-w-0 flex-1">
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
                </div>
                <KeyVaultIllustration size="lg" className="mr-6" />
              </div>
            </Card>
            )}

            <CacheSettings isAdmin={isAdmin} />
          </div>
        </TabsContent>

        {isPartner && (
        <TabsContent value="dashboard-host" className="space-y-6">
          {isAdmin && <ReleaseStatusCard />}
          <SystemResourceUsagePanel />
        </TabsContent>
        )}

        {/* A Partner edits its organizations' branding; an Administrator also edits the
            deployment default, which is what it sees itself (backend services/branding/). */}
        {isPartner && (
        <TabsContent value="personalization">
          <OrgPersonalizationSettings />
        </TabsContent>
        )}

        {isPartner && (
        <TabsContent value="integrations" className="space-y-6">
          {/* API keys, Synced Organizations and Desktop Control as sections (?section=). */}
          <IntegrationsSections />
        </TabsContent>
        )}

        {isPartner && (
        <TabsContent value="user-groups">
          <UserGroupsSettings />
        </TabsContent>
        )}

      </Tabs>
    </div>
  );
}
