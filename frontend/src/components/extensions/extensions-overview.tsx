"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { currentQuery, replaceQuery } from "@/lib/url-state";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { BackLink } from "@/components/ui/back-link";
import { Button } from "@/components/ui/button";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { RefreshButton } from "@/components/ui/refresh-button";
import { VaultUnavailableNotice } from "@/components/ui/vault-unavailable-notice";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { AvailableAddons } from "@/components/extensions/available-addons";
import { CredentialRevealDialog } from "@/components/extensions/credential-reveal-dialog";
import { DraftsList } from "@/components/extensions/drafts-list";
import { InstalledTable } from "@/components/extensions/installed-table";
import { PublishAddonDialog } from "@/components/extensions/publish-addon-dialog";
import { InstallConsentDialog } from "@/components/extensions/install-consent-dialog";
import { useToast } from "@/hooks/use-toast";
import { extensionsAPI } from "@/lib/extensions-api";
import type {
  ExtensionDetail,
  ExtensionInstall,
  ExtensionInstallPreview,
  ExtensionInstallResponse,
  ExtensionManifestInput,
} from "@/types/api";

/** Duck-typed: instanceof breaks across Next chunks, so the status is read off the error. */
function isVaultUnavailable(error: unknown): boolean {
  return (
    error instanceof Error &&
    "status" in error &&
    (error as unknown as Record<string, unknown>).status === 503
  );
}

/**
 * The query key the organization picker rides in.
 *
 * note: the page and page size deliberately stay out of the URL, and the reason is recorded in
 * documentation/ui/project-grid-pagination.md: a page number resolves against the reader's page
 * size and sort rather than the sender's, so a shared link lands somewhere else. Which organization
 * you are installing into describes what you are looking at, so it belongs there.
 */
const ORG_PARAM = "org";

function ExtensionsOverviewContent() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const organizationId = searchParams.get(ORG_PARAM) ?? "";
  const setOrganizationId = useCallback(
    (next: string) => {
      const params = currentQuery();
      if (next) params.set(ORG_PARAM, next);
      else params.delete(ORG_PARAM);
      // replace, not push: a picker change is not a navigation an operator wants Back to undo one
      // selection at a time, and the filter engine writes here the same way.
      replaceQuery(pathname, params);
    },
    [pathname],
  );

  const [preview, setPreview] = useState<ExtensionInstallPreview | null>(null);
  const [issued, setIssued] = useState<ExtensionInstallResponse | null>(null);
  const [installError, setInstallError] = useState<string | null>(null);
  const [pendingUninstall, setPendingUninstall] = useState<ExtensionInstall | null>(null);
  const [uninstallError, setUninstallError] = useState<string | null>(null);

  // note: only the organizations the caller may install into. The general list included ones it is a
  // buyer or a plain member of, where every install answers "Organization not found".
  const organizations = useQuery({
    queryKey: ["extensions", "manageable-organizations"],
    queryFn: () => extensionsAPI.manageableOrganizations(),
    staleTime: 60000,
  });

  // A shared or bookmarked ?org= that this caller may not install into is cleared, not kept selected.
  useEffect(() => {
    if (!organizations.data || !organizationId) return;
    if (!organizations.data.some((org) => String(org.id) === organizationId)) setOrganizationId("");
  }, [organizations.data, organizationId, setOrganizationId]);

  const catalogue = useQuery({
    queryKey: ["extensions", "catalogue"],
    queryFn: () => extensionsAPI.list(),
    retry: (count, error) => (isVaultUnavailable(error) ? false : count < 2),
  });

  const installs = useQuery({
    queryKey: ["extensions", "installs"],
    queryFn: () => extensionsAPI.listInstalls(),
    retry: (count, error) => (isVaultUnavailable(error) ? false : count < 2),
  });

  const beginInstall = useMutation({
    mutationFn: (slug: string) => extensionsAPI.installPreview(slug, Number(organizationId)),
    onSuccess: (result) => {
      setInstallError(null);
      setPreview(result);
    },
    onError: (error: Error) =>
      toast({ title: "Could not read the preview", description: error.message, variant: "destructive" }),
  });

  const confirmInstall = useMutation({
    mutationFn: () => {
      if (!preview) throw new Error("Nothing to install");
      return extensionsAPI.install(preview.extension.slug, {
        organization_id: preview.organization_id,
        consent: {
          version: preview.version,
          // Echoed back so the server can refuse if the manifest moved while this was open.
          manifest_digest: preview.manifest_digest,
          accepted_capabilities: preview.capabilities.map((c) => c.name),
        },
        capabilities: preview.capabilities.map((c) => c.name),
        cluster_permissions: preview.grantable_cluster_ids.map((cluster_id) => ({
          cluster_id,
          permissions: [String(preview.requested_scope?.permission ?? "read")],
          projects: ["default"],
        })),
      });
    },
    onSuccess: (result) => {
      setPreview(null);
      setIssued(result);
      queryClient.invalidateQueries({ queryKey: ["extensions", "installs"] });
    },
    onError: (error: Error) => setInstallError(error.message),
  });

  const vocabulary = useQuery({
    queryKey: ["extensions", "vocabulary"],
    queryFn: () => extensionsAPI.vocabulary(),
    staleTime: 5 * 60_000,
  });

  const [publishOpen, setPublishOpen] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);
  const publish = useMutation({
    mutationFn: (manifest: ExtensionManifestInput) => extensionsAPI.create(manifest),
    onSuccess: () => {
      setPublishOpen(false);
      setPublishError(null);
      queryClient.invalidateQueries({ queryKey: ["extensions", "catalogue"] });
    },
    // The server's own words. A paraphrase here would hide which field it actually refused.
    onError: (error: Error) => setPublishError(error.message),
  });

  const setStatus = useMutation({
    mutationFn: ({ slug, status }: { slug: string; status: ExtensionDetail["status"] }) =>
      extensionsAPI.setStatus(slug, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["extensions", "catalogue"] }),
    onError: (error: Error) =>
      toast({ title: "Could not change status", description: error.message, variant: "destructive" }),
  });

  const withdraw = useMutation({
    mutationFn: (slug: string) => extensionsAPI.withdraw(slug),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["extensions", "catalogue"] }),
    // A withdraw is refused while an installation still holds it, and that refusal explains why.
    onError: (error: Error) =>
      toast({ title: "Could not withdraw", description: error.message, variant: "destructive" }),
  });

  const setInstallStatus = useMutation({
    mutationFn: ({ id, action }: { id: number; action: "suspend" | "resume" }) =>
      action === "suspend"
        ? extensionsAPI.suspend(id, "Suspended from the installations list")
        : extensionsAPI.resume(id),
    onSuccess: (_result, variables) => {
      queryClient.invalidateQueries({ queryKey: ["extensions", "installs"] });
      queryClient.invalidateQueries({ queryKey: ["extensions", "install", variables.id] });
    },
    onError: (error: Error) =>
      toast({ title: "Could not change the installation", description: error.message, variant: "destructive" }),
  });

  const uninstall = useMutation({
    mutationFn: (id: number) => extensionsAPI.uninstall(id),
    onSuccess: () => {
      setPendingUninstall(null);
      setUninstallError(null);
      queryClient.invalidateQueries({ queryKey: ["extensions", "installs"] });
    },
    onError: (error: Error) => setUninstallError(error.message),
  });

  if (isVaultUnavailable(catalogue.error) || isVaultUnavailable(installs.error)) {
    return <VaultUnavailableNotice surface="Add-on installation" />;
  }

  const orgs = organizations.data ?? [];
  const orgPlaceholder = organizations.isError
    ? "Could not load organizations"
    : !organizations.data
      ? "Loading organizations"
      : orgs.length === 0
        ? "No organization you run"
        : "Choose an organization";
  const installedSlugs = new Set((installs.data ?? []).map((i) => i.extension_slug));
  // A draft is visible only to its owner, which is exactly the step that used to strand people:
  // publishing produced one, the operator saw an empty catalogue, and nobody got an error.
  const drafts = (catalogue.data ?? []).filter((e) => e.status === "draft");
  const installable = (catalogue.data ?? []).filter((e) => e.status !== "draft");

  return (
    <div className="space-y-8">
      {/* The refresh reads the queries this component owns, so the page title lives here rather
          than in the route file. Beside the organization picker it read as a statement about the
          picker; on the title line it reads as one about the page. */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        {/* This route is deliberately absent from the sidebar, so the card in Settings is the only
            way in and this is the only way back. */}
        <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-4">
          <BackLink href="/settings" label="Settings" />
          <div className="min-w-0">
            <h1 className="text-3xl font-bold">Extensions</h1>
            <p className="text-sm text-muted-foreground">
              External applications built against the Orcastra API
            </p>
          </div>
        </div>
        <RefreshButton
          label="Refresh add-ons"
          onRefresh={async () => {
            // Both, because the page shows both and refreshing half of it is what made the
            // Installed list look stale after an install.
            await Promise.all([catalogue.refetch(), installs.refetch()]);
          }}
          lastUpdatedAt={Math.min(catalogue.dataUpdatedAt, installs.dataUpdatedAt) || undefined}
        />
      </div>

      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Available Add-ons</h2>
            <p className="text-sm text-muted-foreground">
              Published add-ons you can install into an organization you run
            </p>
          </div>
          <div className="flex items-end gap-2">
            <div className="space-y-1.5">
              <label htmlFor="install-org" className="text-sm font-medium">
                Install into
              </label>
              <Select
                value={organizationId}
                onValueChange={setOrganizationId}
                disabled={!organizations.data || orgs.length === 0}
              >
                <SelectTrigger id="install-org" className="w-56">
                  <SelectValue placeholder={orgPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {orgs.map((org) => (
                    <SelectItem key={org.id} value={String(org.id)}>
                      {org.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              size="sm"
              onClick={() => {
                setPublishError(null);
                setPublishOpen(true);
              }}
            >
              <Plus className="mr-2 h-4 w-4" aria-hidden="true" />
              Publish Add-on
            </Button>
          </div>
        </div>

        <AvailableAddons
          rows={installable}
          installedSlugs={installedSlugs}
          loading={catalogue.isLoading}
          error={(catalogue.error as Error) ?? null}
          onRetry={() => void catalogue.refetch()}
          organizationId={organizationId}
          onInstall={(slug) => beginInstall.mutate(slug)}
          installPending={beginInstall.isPending}
          onPublish={() => setPublishOpen(true)}
        />
      </section>

      {/* Present only when there is something to say. An always-on empty state cost a full
          EmptyState block of whitespace on every visit for a section most operators never use, and
          rows per screen is the product here. The loading and error cases still render, because
          those were the two states this section genuinely lacked. */}
      {(catalogue.isLoading || catalogue.error || drafts.length > 0) && (
      <section className="space-y-3">
        <div>
          <h2 className="text-lg font-semibold">Your Drafts</h2>
          <p className="text-sm text-muted-foreground">
            A draft is visible only to you. Nobody can install it, and nobody is told it exists,
            until you publish it.
          </p>
        </div>
        <DraftsList
          rows={drafts}
          loading={catalogue.isLoading}
          error={(catalogue.error as Error) ?? null}
          onRetry={() => void catalogue.refetch()}
          onPublish={(slug) => setStatus.mutate({ slug, status: "published" })}
          onDiscard={(slug) => withdraw.mutate(slug)}
          publishPending={setStatus.isPending}
          discardPending={withdraw.isPending}
        />
      </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Installed</h2>
        <InstalledTable
          rows={installs.data ?? []}
          loading={installs.isLoading}
          error={(installs.error as Error) ?? null}
          onRetry={() => void installs.refetch()}
          onSetStatus={(row, action) => setInstallStatus.mutate({ id: row.id, action })}
          onUninstall={(row) => {
            setUninstallError(null);
            setPendingUninstall(row);
          }}
          actionPending={setInstallStatus.isPending}
        />
      </section>

      <PublishAddonDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        vocabulary={vocabulary.data}
        pending={publish.isPending}
        error={publishError}
        onSubmit={(manifest) => publish.mutate(manifest)}
      />

      <InstallConsentDialog
        preview={preview}
        open={preview !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPreview(null);
            setInstallError(null);
          }
        }}
        onConfirm={() => confirmInstall.mutate()}
        pending={confirmInstall.isPending}
        error={installError}
      />

      <DestructiveConfirmDialog
        open={pendingUninstall !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingUninstall(null);
            setUninstallError(null);
          }
        }}
        title={`Uninstall ${pendingUninstall?.extension_name ?? ""}?`}
        description={
          <>
            The credential this installation issued stops working immediately, and its webhook
            subscriptions and stored settings are removed. Anything the add-on created on a cluster
            is left alone. This cannot be undone, and reinstalling issues a new credential rather
            than restoring this one.
          </>
        }
        confirmLabel="Uninstall"
        pendingLabel="Uninstalling"
        pending={uninstall.isPending}
        onConfirm={() => {
          if (pendingUninstall) uninstall.mutate(pendingUninstall.id);
        }}
        error={uninstallError}
      />

      <CredentialRevealDialog
        result={issued}
        open={issued !== null}
        onClose={() => setIssued(null)}
      />
    </div>
  );
}

/**
 * The boundary the query string requires.
 *
 * The organization picker lives in the query string, which opts this out of prerendering, and Next
 * fails `next build` on the route without a boundary above it. It sits here rather than in the
 * route file so the component carries its own requirement.
 */
export function ExtensionsOverview() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <ExtensionsOverviewContent />
    </Suspense>
  );
}
