"use client";

import { Suspense, useCallback, useRef } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { replaceUrl } from "@/lib/url-state";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RotateCcw } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { BackLink } from "@/components/ui/back-link";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { DisconnectedIllustration } from "@/components/illustrations/scenes/states";
import { RefreshButton } from "@/components/ui/refresh-button";
import { NodeLink } from "@/components/ui/node-link";
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
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
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

  // The panel lives in the URL, so a link to a delivery log lands on the delivery log and a
  // refresh keeps the open tab.
  const raw = search.get("panel");
  const panel: Panel = PANELS.includes(raw as Panel) ? (raw as Panel) : "overview";
  const setPanel = useCallback(
    (next: string) => replaceUrl(`/extensions/${installId}?panel=${next}`),
    [installId],
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

  // The same query key the webhook panel uses, so react-query serves the health panel and that
  // tab from one fetch. The deliveries query below keeps its own key.
  const subscriptions = useQuery({
    queryKey: ["extensions", "subscriptions", installId],
    queryFn: () => extensionsAPI.listSubscriptions(installId),
  });

  // The health panel reads `counts`, which the server computes over every delivery on the
  // installation. It asks for a single row because it does not render the list; counting over a
  // page in hand would report the newest fifty as the whole history.
  const deliveries = useQuery({
    queryKey: ["extensions", "deliveries", installId, "counts"],
    queryFn: () => extensionsAPI.listDeliveries(installId, { limit: 1 }),
  });

  // The whole extensions family, not only this install: the overview lists the same install's
  // status and the catalogue counts installs, so both must refresh too.
  const invalidate = () => reconcileMutationImpact(queryClient, "extension.write");

  const saveSettings = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      extensionsAPI.updateSettings(installId, values),
    onSuccess: () => {
      toast({ title: "Settings saved" });
      invalidate();
    },
    onError: (error: Error) =>
      toast({ title: "Could not save settings", description: error.message, variant: "destructive" }),
  });

  const rotate = useMutation({
    mutationFn: () => extensionsAPI.rotateCredential(installId, 0),
    onSuccess: (result) => {
      setIssued(result);
      invalidate();
    },
    onError: (error: Error) =>
      toast({ title: "Could not rotate credential", description: error.message, variant: "destructive" }),
  });

  const setStatus = useMutation({
    mutationFn: (next: "suspend" | "resume") =>
      next === "suspend" ? extensionsAPI.suspend(installId) : extensionsAPI.resume(installId),
    onSuccess: invalidate,
  });

  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [uninstallError, setUninstallError] = useState<string | null>(null);
  // important: guarded by a ref instead of `isPending`. The shared dialog blocks its confirm on
  // React state, which reaches the DOM only on the next render, so two clicks in one tick would
  // both get through. A ref claimed before any await, as in the consent dialog.
  const uninstalling = useRef(false);
  const uninstall = useMutation({
    mutationFn: () => extensionsAPI.uninstall(installId),
    onSuccess: () => {
      setConfirmUninstall(false);
      void reconcileMutationImpact(queryClient, "extension.write");
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
          illustration={DisconnectedIllustration}
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
        <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-4">
          <BackLink href="/extensions" label="Extensions" />
          <div className="min-w-0">
            <h1 className="text-3xl font-bold">{row.extension_name}</h1>
            <p className="text-sm text-muted-foreground">
              v{row.extension_version} in {row.organization_slug ?? row.organization_id}
            </p>
          </div>
        </div>
        {/* Actions only. The status is in the Health panel, beside the notice explaining a
            suspension; next to these buttons it would read as a third one. */}
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
              {/* The reach an add-on was granted. Each id links to its cluster, so "which
                  cluster is that" is answered without leaving the page. */}
              {row.granted_scope.map((grant) => (
                <li key={String(grant.cluster_id)}>
                  <NodeLink clusterId={String(grant.cluster_id)} className="font-mono text-sm" />
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
