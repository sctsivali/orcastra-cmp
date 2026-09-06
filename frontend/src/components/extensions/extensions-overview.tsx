"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Puzzle } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { TableLink } from "@/components/ui/table-link";
import { VaultUnavailableNotice } from "@/components/ui/vault-unavailable-notice";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CredentialRevealDialog } from "@/components/extensions/credential-reveal-dialog";
import { PublishAddonDialog } from "@/components/extensions/publish-addon-dialog";
import { InstallConsentDialog } from "@/components/extensions/install-consent-dialog";
import { useToast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { extensionsAPI } from "@/lib/extensions-api";
import { INSTALL_STATUS, describeClusterReach } from "@/lib/extension-display";
import type {
  ExtensionDetail,
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

export function ExtensionsOverview() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [organizationId, setOrganizationId] = useState<string>("");
  const [preview, setPreview] = useState<ExtensionInstallPreview | null>(null);
  const [issued, setIssued] = useState<ExtensionInstallResponse | null>(null);
  const [installError, setInstallError] = useState<string | null>(null);

  const organizations = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.organizations.list(),
    staleTime: 60000,
  });

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
    mutationFn: (slug: string) =>
      extensionsAPI.installPreview(slug, Number(organizationId)),
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

  if (isVaultUnavailable(catalogue.error) || isVaultUnavailable(installs.error)) {
    return <VaultUnavailableNotice surface="Add-on installation" />;
  }

  const orgs = organizations.data ?? [];
  const installedSlugs = new Set((installs.data ?? []).map((i) => i.extension_slug));
  // A draft is visible only to its owner, which is exactly the step that used to strand people:
  // publishing produced one, the operator saw an empty catalogue, and nobody got an error.
  const drafts = (catalogue.data ?? []).filter((e) => e.status === "draft");
  const installable = (catalogue.data ?? []).filter((e) => e.status !== "draft");

  return (
    <div className="space-y-8">
      {/* The refresh reads the catalogue query this component owns, so the page title lives here
          rather than in the route file. Beside the organization picker it read as a statement
          about the picker; on the title line it reads as one about the page. */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold">Extensions</h1>
          <p className="text-sm text-muted-foreground">
            External applications built against the Orcastra API
          </p>
        </div>
        <RefreshButton
          onRefresh={() => catalogue.refetch()}
          lastUpdatedAt={catalogue.dataUpdatedAt}
        />
      </div>

      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Available Add-ons</h2>
            <p className="text-sm text-muted-foreground">
              Published add-ons you can install into an organization you hold
            </p>
          </div>
          <div className="flex items-end gap-2">
            <div className="space-y-1.5">
              <label htmlFor="install-org" className="text-sm font-medium">
                Install into
              </label>
              <Select value={organizationId} onValueChange={setOrganizationId}>
                <SelectTrigger id="install-org" className="w-56">
                  <SelectValue placeholder="Choose an organization" />
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

        {catalogue.isLoading ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="h-24 animate-pulse rounded-lg bg-muted" />
            <div className="h-24 animate-pulse rounded-lg bg-muted" />
          </div>
        ) : catalogue.error ? (
          <Alert variant="destructive">
            <AlertDescription>{(catalogue.error as Error).message}</AlertDescription>
          </Alert>
        ) : installable.length === 0 ? (
          <EmptyState
            icon={Puzzle}
            title="No add-ons are published yet"
            description="An add-on is an external application built against the Orcastra API. Publishing one makes it installable here."
            action={
              <Button size="sm" onClick={() => setPublishOpen(true)}>
                <Plus className="mr-2 h-4 w-4" aria-hidden="true" />
                Publish Add-on
              </Button>
            }
          />
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2">
            {installable.map((extension) => (
              <li key={extension.slug} className="rounded-xl border bg-card p-4">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h3 className="font-semibold">{extension.name}</h3>
                    <p className="text-xs text-muted-foreground">
                      {extension.publisher} · v{extension.version}
                    </p>
                  </div>
                  {installedSlugs.has(extension.slug) ? (
                    <Badge variant="muted">Installed</Badge>
                  ) : null}
                </div>
                {extension.description ? (
                  <p className="mt-2 text-sm text-muted-foreground">{extension.description}</p>
                ) : null}
                <p className="mt-2 text-xs text-muted-foreground">
                  Asks for {extension.requested_capabilities.length} capabilit
                  {extension.requested_capabilities.length === 1 ? "y" : "ies"}
                </p>
                <Button
                  size="sm"
                  className="mt-3"
                  disabled={!organizationId || beginInstall.isPending}
                  onClick={() => beginInstall.mutate(extension.slug)}
                >
                  Review And Install
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {drafts.length > 0 && (
        <section className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold">Your Drafts</h2>
            <p className="text-sm text-muted-foreground">
              A draft is visible only to you. Nobody can install it, and nobody is told it exists,
              until you publish it.
            </p>
          </div>
          <ul className="divide-y rounded-lg border">
            {drafts.map((extension) => (
              <li key={extension.slug} className="flex flex-wrap items-center gap-3 px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="font-medium">{extension.name}</p>
                  <p className="text-xs text-muted-foreground">
                    <span className="font-mono">{extension.slug}</span> · v{extension.version} ·
                    asks for {extension.requested_capabilities.length} capabilit
                    {extension.requested_capabilities.length === 1 ? "y" : "ies"}
                  </p>
                </div>
                <Badge variant="caution">Draft</Badge>
                <Button
                  size="sm"
                  disabled={setStatus.isPending}
                  onClick={() => setStatus.mutate({ slug: extension.slug, status: "published" })}
                >
                  Publish
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={withdraw.isPending}
                  onClick={() => withdraw.mutate(extension.slug)}
                >
                  Discard
                </Button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Installed</h2>
        {installs.isLoading ? (
          <div className="h-24 animate-pulse rounded-lg bg-muted" />
        ) : (installs.data ?? []).length === 0 ? (
          <EmptyState
            title="Nothing is installed yet"
            description="Installing an add-on issues it a credential scoped to one organization."
          />
        ) : (
          <ul className="divide-y rounded-lg border">
            {(installs.data ?? []).map((row) => {
              const meta = INSTALL_STATUS[row.status];
              const clusters = row.granted_scope
                .map((g) => String(g.cluster_id))
                .filter(Boolean);
              return (
                <li key={row.id} className="flex flex-wrap items-center gap-3 px-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <TableLink href={`/extensions/${row.id}`}>{row.extension_name}</TableLink>
                    <p className="text-xs text-muted-foreground">
                      {row.organization_slug ?? `organization ${row.organization_id}`} ·{" "}
                      {describeClusterReach(clusters)} · {row.granted_capabilities.length}{" "}
                      capabilit{row.granted_capabilities.length === 1 ? "y" : "ies"}
                    </p>
                  </div>
                  <Badge variant={meta.tone}>{meta.label}</Badge>
                </li>
              );
            })}
          </ul>
        )}
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

      <CredentialRevealDialog
        result={issued}
        open={issued !== null}
        onClose={() => setIssued(null)}
      />
    </div>
  );
}
