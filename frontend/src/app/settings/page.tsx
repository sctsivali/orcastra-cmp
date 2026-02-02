"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Trash2, Check, Key, ChevronRight, Activity, Settings2, Palette, Shield } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { SystemResourceUsagePanel } from "@/components/system-resource-usage";
import { PersonalizationSettings } from "@/components/personalization-settings";
import { IntegrationsSettings } from "@/components/integrations-settings";

export default function SettingsPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [isClearing, setIsClearing] = useState(false);
  const [cleared, setCleared] = useState(false);

  const handleClearCache = async () => {
    setIsClearing(true);
    setCleared(false);
    
    try {
      // Clear backend cache (Redis + in-memory)
      try {
        await fetch('/api/system/cache/clear', { method: 'POST' });
      } catch (backendError) {
        console.warn("Backend cache clear failed (may be unavailable):", backendError);
      }
      
      // Clear all React Query cache (frontend)
      await queryClient.invalidateQueries();
      queryClient.clear();
      
      // Clear any localStorage cache keys if exist
      if (typeof window !== 'undefined') {
        const keysToRemove = Object.keys(localStorage).filter(
          key => key.startsWith('cache_') || key.startsWith('query_')
        );
        keysToRemove.forEach(key => localStorage.removeItem(key));
      }
      
      setCleared(true);
      
      // Reset the cleared state after 3 seconds
      setTimeout(() => {
        setCleared(false);
      }, 3000);
    } catch (error) {
      console.error("Failed to clear cache:", error);
    } finally {
      setIsClearing(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Settings</h1>
        <p className="text-muted-foreground mt-2">
          Configure system preferences, manage cache, and monitor resource usage
        </p>
      </div>

      <Tabs defaultValue="general" className="space-y-6">
        <TabsList className="inline-flex w-auto h-auto p-1">
          <TabsTrigger value="general" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Settings2 className="h-4 w-4 shrink-0" />
            <span>General</span>
          </TabsTrigger>
          <TabsTrigger value="integrations" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Shield className="h-4 w-4 shrink-0" />
            <span>Integrations</span>
          </TabsTrigger>
          <TabsTrigger value="personalization" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Palette className="h-4 w-4 shrink-0" />
            <span>Personalization</span>
          </TabsTrigger>
          <TabsTrigger value="system-info" className="gap-2 text-xs sm:text-sm px-3 sm:px-4 py-1.5">
            <Activity className="h-4 w-4 shrink-0" />
            <span>System Info</span>
          </TabsTrigger>
        </TabsList>

        {/* General Settings Tab */}
        <TabsContent value="general" className="space-y-6">
          <div className="grid gap-6">
            {/* My Keys Card */}
            <Card className="hover:shadow-lg transition-shadow cursor-pointer" onClick={() => router.push('/my-keys')}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Key className="h-5 w-5" />
                  My Keys
                </CardTitle>
                <CardDescription>
                  Manage TLS identity keys for accessing hypervisor resources
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <p className="text-sm text-muted-foreground">
                    Generate and manage TLS certificates for secure access to your hypervisor nodes and projects. 
                    Generate keys with custom permissions and expiration dates.
                  </p>
                  <Button 
                    variant="default" 
                    className="gap-2 mt-2"
                    onClick={(e) => {
                      e.stopPropagation();
                      router.push('/my-keys');
                    }}
                  >
                    Manage Keys
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </div>
              </CardContent>
            </Card>

            {/* Cache Management Card */}
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Trash2 className="h-5 w-5" />
                  Cache Management
                </CardTitle>
                <CardDescription>
                  Manage application data cache to ensure fresh data retrieval
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <h3 className="text-sm font-semibold">Clear Application Cache</h3>
                  <p className="text-sm text-muted-foreground">
                    Clear all cached data from both frontend (React Query) and backend (Redis). 
                    This includes nodes, instances, clusters, networks, and storage information. 
                    Fresh data will be fetched on the next request.
                  </p>
                  <div className="flex items-center gap-3 pt-2">
                    <Button
                      variant="destructive"
                      onClick={handleClearCache}
                      disabled={isClearing}
                      className="gap-2"
                    >
                      {isClearing ? (
                        <>
                          <Trash2 className="h-4 w-4 animate-spin" />
                          Clearing Cache...
                        </>
                      ) : cleared ? (
                        <>
                          <Check className="h-4 w-4" />
                          Cache Cleared
                        </>
                      ) : (
                        <>
                          <Trash2 className="h-4 w-4" />
                          Clear All Cache
                        </>
                      )}
                    </Button>
                    {cleared && (
                      <Badge variant="outline" className="bg-green-500/10 text-green-500 border-green-500/20">
                        Successfully cleared all cached data
                      </Badge>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        {/* System Info Tab */}
        <TabsContent value="system-info">
          <SystemResourceUsagePanel />
        </TabsContent>

        {/* Personalization Tab */}
        <TabsContent value="personalization">
          <PersonalizationSettings />
        </TabsContent>

        {/* Integrations Tab */}
        <TabsContent value="integrations">
          <IntegrationsSettings />
        </TabsContent>
      </Tabs>
    </div>
  );
}
