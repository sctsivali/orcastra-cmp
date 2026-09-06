"use client";

import { Suspense, useCallback, useRef } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RotateCcw } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { AddonHealthPanel } from "@/components/extensions/addon-health-panel";
import { CredentialRevealDialog } from "@/components/extensions/credential-reveal-dialog";
import { ExtensionSettingsForm } from "@/components/extensions/extension-settings-form";
import { WebhookPanel } from "@/components/extensions/webhook-panel";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { useToast } from "@/hooks/use-toast";
import { extensionsAPI } from "@/lib/extensions-api";
import { Role } from "@/types/rbac";
import type { ExtensionInstallResponse, WebhookSecretResponse } from "@/types/api";

const PANELS = ["overview", "settings", "webhooks"] as const;
type Panel = (typeof PANELS)[number];

function InstallDetail() {
  const params = useParams<{ installId: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const installId = Number(params.installId);
  const [issued, setIssued] = useState<ExtensionInstallResponse | null>(null);

  // important: the panel lives in the URL. Settings cannot deep-link to a tab and a refresh
  // drops you back to the first one, which is the limitation this surface deliberately does not
  // inherit: an operator sent a link to a delivery log should land on the delivery log.
  const raw = search.get("panel");
  const panel: Panel = PANELS.includes(raw as Panel) ? (raw as Panel) : "overview";
  const setPanel = useCallback(
    (next: string) => router.replace(`/extensions/${installId}?panel=${next}`, { scroll: false }),
    [router, installId],
  );

  const install = useQuery({
    queryKey: ["extensions", "install", installId],
    queryFn: () => extensionsAPI.getInstall(installId),
    enabled: Number.isFinite(installId),
  });

  // The settings form is drawn from the add-on's declared schema, which lives on the
  // definition rather than on the installation. The installation carries the values.
  const definition = useQuery({
    queryKey: ["extensions", "definition", install.data?.extension_slug],
    queryFn: () => extensionsAPI.get(install.data!.extension_slug),
    enabled: Boolean(install.data?.extension_slug),
    staleTime: 60000,
  });

  const vocabulary = useQuery({
    queryKey: ["extensions", "vocabulary"],
    queryFn: () => extensionsAPI.vocabulary(),
    staleTime: 3600000,
  });

  // note: the same query key the webhook panel uses, so react-query serves both from one fetch
  // rather than the health panel doubling the request the tab below it already makes. The
  // deliveries query below deliberately does not share a key any more, and says why.
  const subscriptions = useQuery({
    queryKey: ["extensions", "subscriptions", installId],
    queryFn: () => extensionsAPI.listSubscriptions(installId),
  });

  // The health panel reads `counts`, which the server computes over every delivery on the
  // installation. It asks for a single row because it does not render the list: reducing over
  // whatever page happened to be in hand is exactly how it came to report the newest fifty as the
  // whole history.
  const deliveries = useQuery({
    queryKey: ["extensions", "deliveries", installId, "counts"],
    queryFn: () => extensionsAPI.listDeliveries(installId, { limit: 1 }),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["extensions", "install", installId] });

  const saveSettings = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      extensionsAPI.updateSettings(installId, values),
    onSuccess: () => {
      toast({ title: "Settings saved" });
      invalidate();
    },
    onError: (error: Error) =>
      toast({ title: "Could not save", description: error.message, variant: "destructive" }),
  });

  const rotate = useMutation({
    mutationFn: () => extensionsAPI.rotateCredential(installId, 0),
    onSuccess: (result) => {
      setIssued(result);
      invalidate();
    },
    onError: (error: Error) =>
      toast({ title: "Could not rotate", description: error.message, variant: "destructive" }),
  });

  const setStatus = useMutation({
    mutationFn: (next: "suspend" | "resume") =>
      next === "suspend" ? extensionsAPI.suspend(installId) : extensionsAPI.resume(installId),
    onSuccess: invalidate,
  });

  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [uninstallError, setUninstallError] = useState<string | null>(null);
  // important: a ref, not `isPending`. The shared dialog blocks its confirm on React state, which
  // does not reach the DOM until the next render, so two clicks in one tick both get through. The
  // house pattern for this is a ref claimed before any await, the same as the consent dialog.
  const uninstalling = useRef(false);
  const uninstall = useMutation({
    mutationFn: () => extensionsAPI.uninstall(installId),
    onSuccess: () => {
      setConfirmUninstall(false);
      queryClient.invalidateQueries({ queryKey: ["extensions", "installs"] });
      router.push("/extensions");
    },
    onError: (error: Error) => {
      uninstalling.current = false;
      setUninstallError(error.message);
    },
  });

  if (install.isLoading) return <LoadingLottiePage />;
  if (install.error) {
    return (
      <div className="container mx-auto p-6">
        <EmptyState
          icon={RotateCcw}
          title="Could not load this installation"
          description={(install.error as Error).message}
          action={
            <Button size="sm" onClick={() => void install.refetch()}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }

  const row = install.data;
  if (!row) return null;

  return (
    <div className="container mx-auto space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold">{row.extension_name}</h1>
          <p className="text-sm text-muted-foreground">
            v{row.extension_version} in {row.organization_slug ?? row.organization_id}
          </p>
        </div>
        {/* Actions only. The status moved into the Health panel, where the notice explaining a
            suspension already lives; beside these buttons it read as a third one. */}
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh this installation"
            onRefresh={async () => {
              await Promise.all([install.refetch(), subscriptions.refetch(), deliveries.refetch()]);
            }}
            lastUpdatedAt={install.dataUpdatedAt}
          />
          {row.status === "active" ? (
            <Button variant="outline" size="sm" onClick={() => setStatus.mutate("suspend")}>
              Suspend
            </Button>
          ) : row.status === "suspended" ? (
            <Button variant="outline" size="sm" onClick={() => setStatus.mutate("resume")}>
              Resume
            </Button>
          ) : null}
          <Button
            variant="destructive"
            size="sm"
            onClick={() => {
              uninstalling.current = false;
              setUninstallError(null);
              setConfirmUninstall(true);
            }}
          >
            Uninstall
          </Button>
        </div>
      </div>

      <DestructiveConfirmDialog
        open={confirmUninstall}
        onOpenChange={setConfirmUninstall}
        title={`Uninstall ${row.extension_name}?`}
        description={
          <>
            The credential this installation issued stops working immediately, and its webhook
            subscriptions and stored settings are removed. Anything the add-on created on a
            cluster is left alone. This cannot be undone, and reinstalling issues a new
            credential rather than restoring this one.
          </>
        }
        confirmLabel="Uninstall"
        pendingLabel="Uninstalling"
        pending={uninstall.isPending}
        onConfirm={() => {
          if (uninstalling.current) return;
          uninstalling.current = true;
          uninstall.mutate();
        }}
        error={uninstallError}
      />

      <Tabs value={panel} onValueChange={setPanel}>
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
          <TabsTrigger value="webhooks">Webhooks</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4 pt-4">
          <AddonHealthPanel
            install={row}
            subscriptions={subscriptions.data ?? []}
            deliveryCounts={deliveries.data?.counts ?? {}}
            deliveryTotal={deliveries.data?.total ?? 0}
          />

          <section>
            <h2 className="text-lg font-semibold">Granted Capabilities</h2>
            <ul className="mt-2 flex flex-wrap gap-2">
              {row.granted_capabilities.map((name) => (
                <li key={name}>
                  <Badge variant="muted" className="font-mono">
                    {name}
                  </Badge>
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold">Clusters</h2>
            <ul className="mt-2 flex flex-wrap gap-2">
              {row.granted_scope.map((grant) => (
                <li key={String(grant.cluster_id)}>
                  <Badge variant="muted" className="font-mono">
                    {String(grant.cluster_id)}
                  </Badge>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-sm text-muted-foreground">
              Reach is recomputed on every call, so narrowing your own access narrows this too.
            </p>
          </section>

          <section className="space-y-2">
            <h2 className="text-lg font-semibold">Credential</h2>
            <p className="text-sm text-muted-foreground">
              Key <span className="font-mono">{row.api_key_id}</span>, generation{" "}
              {row.credential_generation}. The secret is not readable. If it was lost, rotate.
            </p>
            <Button variant="outline" size="sm" onClick={() => rotate.mutate()} disabled={rotate.isPending}>
              Rotate Credential
            </Button>
          </section>
        </TabsContent>

        <TabsContent value="settings" className="pt-4">
          <ExtensionSettingsForm
            schema={definition.data?.settings_schema ?? []}
            values={row.settings}
            configuredSecrets={row.configured_secrets}
            onSave={(values) => saveSettings.mutate(values)}
            pending={saveSettings.isPending}
          />
        </TabsContent>

        <TabsContent value="webhooks" className="pt-4">
          <WebhookPanel
            installId={installId}
            vocabulary={vocabulary.data}
            onSecretIssued={(secret: WebhookSecretResponse) =>
              toast({
                title: "Signing secret issued",
                description: `Fingerprint ${secret.secret_fingerprint}. It is not shown again.`,
              })
            }
          />
        </TabsContent>
      </Tabs>

      <CredentialRevealDialog
        result={issued}
        open={issued !== null}
        onClose={() => setIssued(null)}
      />
    </div>
  );
}

export default function InstallDetailPage() {
  const { allowed, isResolved } = useRequireRole(Role.PARTNER);
  if (!isResolved) return <LoadingLottiePage />;
  if (!allowed) return null;
  // useSearchParams needs a boundary, or the whole route opts into client rendering.
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <InstallDetail />
    </Suspense>
  );
}
