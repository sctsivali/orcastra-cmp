"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CacheIllustration } from "@/components/illustrations/scenes/catalog";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { captureActorMutationFence, isActorMutationFenceCurrent } from "@/lib/actor-mutation-fence";
import { api } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { clearBrowserReadCache } from "./browser-cache";

type Result = { kind: "success" | "error"; message: string } | null;

export function CacheSettings({ isAdmin }: { isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  const busyRef = useRef(false);
  const [busy, setBusy] = useState<"browser" | "shared" | null>(null);
  const [result, setResult] = useState<Result>(null);

  useEffect(() => {
    busyRef.current = false;
    setBusy(null);
    setResult(null);
  }, [scopeKey]);

  const clear = async (target: "browser" | "shared") => {
    if (busyRef.current || (target === "shared" && !isAdmin)) return;
    const fence = captureActorMutationFence(scopeKey);
    if (!isActorMutationFenceCurrent(fence)) return;
    busyRef.current = true;
    setBusy(target);
    setResult(null);
    try {
      if (target === "shared") {
        await api.system.clearCache();
        if (!isActorMutationFenceCurrent(fence)) return;
        await reconcileMutationImpact(queryClient, "node.cache");
      } else {
        const applied = await clearBrowserReadCache(queryClient, () => isActorMutationFenceCurrent(fence));
        if (!applied) return;
      }
      if (!isActorMutationFenceCurrent(fence)) return;
      setResult({
        kind: "success",
        message: target === "browser"
          ? "Browser cache cleared. Active views have refreshed their data."
          : "Shared backend cache cleared.",
      });
    } catch (error) {
      if (!isActorMutationFenceCurrent(fence)) return;
      setResult({
        kind: "error",
        message: error instanceof Error ? error.message : "Cache could not be cleared. Try again.",
      });
    } finally {
      if (isActorMutationFenceCurrent(fence)) {
        busyRef.current = false;
        setBusy(null);
      }
    }
  };

  return (
    <Card className="illo-launch">
      <div className="flex items-center">
        <div className="min-w-0 flex-1">
          <CardHeader>
            <CardTitle>Cache Management</CardTitle>
            <CardDescription>Refresh cached application data</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="space-y-3">
              <h3 className="text-sm font-semibold">Browser Cache</h3>
              <p className="text-sm text-muted-foreground">
                Clear inventory and monitoring data in this browser. Your sign-in, preferences, and active operations stay available.
              </p>
              <Button disabled={busy !== null} onClick={() => void clear("browser")}>
                {busy === "browser" ? "Clearing Browser Cache…" : "Clear Browser Cache"}
              </Button>
            </div>
            {isAdmin && (
              <div className="space-y-3 border-t pt-6">
                <h3 className="text-sm font-semibold">Shared Backend Cache</h3>
                <p className="text-sm text-muted-foreground">
                  Clear shared caches for all organizations. Active application views refresh after the backend confirms completion.
                </p>
                <Button variant="destructive" disabled={busy !== null} onClick={() => void clear("shared")}>
                  {busy === "shared" ? "Clearing Shared Cache…" : "Clear Shared Backend Cache"}
                </Button>
              </div>
            )}
            {result && (
              <Alert variant={result.kind === "error" ? "destructive" : "default"} role={result.kind === "error" ? "alert" : "status"}>
                <AlertDescription>{result.message}</AlertDescription>
              </Alert>
            )}
          </CardContent>
        </div>
      <CacheIllustration size="lg" className="mr-6" />
      </div>
    </Card>
  );
}
