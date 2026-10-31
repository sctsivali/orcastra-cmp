"use client";

import * as React from "react";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { IntentLink as Link } from "@/components/navigation/route-intent";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useToast } from "@/hooks/use-toast";
import {
  useNetwork,
  useNetworkState,
  useDeleteNetwork,
} from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { NetworkInfo } from "@/types/api";
import { formatBytes, EMPTY_VALUE, formatLabel } from "@/lib/utils";
import { networkStatusDisplay } from "@/lib/resource-status";
import { AlertTriangle, Network, Box, Layers, Trash2, Loader2 } from "lucide-react";

interface NetworkDetailProps {
  name: string;
  host: string;
  /** The LXD project holding this network. Omit for the default project. */
  project?: string;
}

interface UsedByRef {
  kind: string; // "instances" | "profiles" | ...
  name: string;
  project?: string;
}

/** Branches drawn on the trunk at once. Ten is already a tall diagram. */
const TOPOLOGY_PAGE_SIZE = PANEL_PAGE_SIZE;

function parseUsedBy(url: string): UsedByRef {
  const [path, query] = url.split("?");
  const parts = path.split("/").filter(Boolean); // ["1.0","instances","name"]
  const kind = parts[1] || "";
  const name = decodeURIComponent(parts[parts.length - 1] || "");
  const project =
    new URLSearchParams(query || "").get("project") || undefined;
  return { kind, name, project };
}

/** Wrap the shared formatBytes (lib/utils) so RX/TX render the empty value when absent. */
function formatCounter(bytes?: number): string | null {
  if (bytes == null || Number.isNaN(bytes)) return null;
  return formatBytes(bytes);
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-[180px_1fr] gap-1 sm:gap-4 py-2 border-b last:border-b-0">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="text-sm">{children}</div>
    </div>
  );
}

export function NetworkDetail({ name, host, project }: NetworkDetailProps) {
  // note: navigate() rather than a bare push, so the leaving transition raises the
  // global progress bar. These hops follow a mutation and are the slowest in the app.
  const { navigate } = useNavigationProgress();
  const { toast } = useToast();
  const [topologyPage, setTopologyPage] = React.useState(1);
  const { data, isLoading, error } = useNetwork(name, host, project);
  const network = data as NetworkInfo | undefined;
  const { data: stateData } = useNetworkState(name, host, project);
  const deleteNetwork = useDeleteNetwork();
  const { isReadOnly } = useReadOnlyMode();
  const { hasRole } = usePermissions();
  const canDelete = hasRole(Role.PARTNER) && !isReadOnly;

  if (isLoading) return <LoadingLottiePage />;

  if (error || !network) {
    return (
      <div className="space-y-4">
        <Link href="/inventory/networks" className="text-sm text-primary hover:underline">
          &larr; Back to Networks
        </Link>
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden />
          <AlertDescription>
            Failed to load network &quot;{name}&quot; on {host}.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  // LXD refuses to delete unmanaged networks (a physical NIC, `lo`, ...), so
  // don't offer a button that is guaranteed to fail.
  const canDeleteThis = canDelete && network.managed;

  const config: Record<string, string> = network.config || {};
  const refs = (network.used_by || []).map(parseUsedBy);
  const instances = refs.filter((r) => r.kind === "instances");
  const profiles = refs.filter((r) => r.kind === "profiles");
  const others = refs.filter(
    (r) => r.kind !== "instances" && r.kind !== "profiles",
  );

  // The trunk drew one branch per entry in used_by, which on a default bridge is every instance
  // attached to it. A topology with two hundred branches has stopped being a diagram, so the
  // branches are paged and the total moves onto the card title.
  const branches = [...instances, ...profiles, ...others];
  const branchPages = Math.max(1, Math.ceil(branches.length / TOPOLOGY_PAGE_SIZE));
  const branchPage = Math.min(topologyPage, branchPages);
  const pagedBranches = branches.slice(
    (branchPage - 1) * TOPOLOGY_PAGE_SIZE,
    branchPage * TOPOLOGY_PAGE_SIZE,
  );

  const state = (stateData as any)?.state;
  const counters = state?.counters || {};
  const rx = formatCounter(counters.bytes_received);
  const tx = formatCounter(counters.bytes_sent);

  const handleDelete = async () => {
    try {
      // important: with the project. Without it the delete addresses the default project,
      // where this network either does not exist or is a different one with the same name.
      await deleteNetwork.mutateAsync({ name, host, project });
      toast({ title: "Network deleted", description: `"${name}" was deleted.` });
      navigate("/inventory/networks");
    } catch (e) {
      toast({
        title: "Could not delete network",
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    }
  };

  const refBadge = (r: UsedByRef, icon: React.ReactNode) => (
    <Badge
      key={`${r.kind}-${r.project || ""}-${r.name}`}
      variant="secondary"
      className="gap-1 font-normal"
    >
      {icon}
      {r.name}
    </Badge>
  );

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Breadcrumb header */}
      <div className="flex items-center justify-between">
        <div className="text-2xl font-bold flex items-center gap-2">
          <Link href="/inventory/networks" className="text-primary hover:underline">
            Networks
          </Link>
          <span className="text-muted-foreground">/</span>
          <span>{network.name}</span>
        </div>
        {canDeleteThis && (
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="destructive" size="sm">
                <Trash2 className="h-4 w-4 mr-2" />
                Delete network
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete Network &quot;{network.name}&quot;?</AlertDialogTitle>
                <AlertDialogDescription>
                  This permanently deletes the network on {host}. Networks that are
                  still in use by instances or profiles cannot be deleted.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  onClick={handleDelete}
                  className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                >
                  {deleteNetwork.isPending && (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  )}
                  Delete
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        )}
      </div>

      {/* Connections / topology */}
      <Card>
        <CardHeader>
          <CardTitle>Connections ({refs.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {refs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              This network is not used by any instance or profile.
            </p>
          ) : (
            <div className="flex items-center overflow-x-auto py-2">
              <Badge variant="outline" className="gap-1 shrink-0">
                <Network className="h-3.5 w-3.5" />
                {network.name}
              </Badge>
              {/* horizontal line from the network to the trunk */}
              <div className="h-px w-6 bg-border shrink-0" />
              <div className="flex flex-col">
                {pagedBranches.map((r, i, arr) => {
                  const isFirst = i === 0;
                  const isLast = i === arr.length - 1;
                  const icon =
                    r.kind === "instances" ? (
                      <Box className="h-3.5 w-3.5" />
                    ) : r.kind === "profiles" ? (
                      <Layers className="h-3.5 w-3.5" />
                    ) : (
                      <Network className="h-3.5 w-3.5" />
                    );
                  return (
                    <div
                      key={`${r.kind}-${r.project || ""}-${r.name}`}
                      className="flex items-center h-9"
                    >
                      {/* connector cell: vertical trunk + horizontal branch */}
                      <div className="relative w-6 self-stretch shrink-0">
                        {arr.length > 1 && (
                          <span
                            className="absolute left-0 w-px bg-border"
                            style={{
                              top: isFirst ? "50%" : 0,
                              bottom: isLast ? "50%" : 0,
                            }}
                          />
                        )}
                        <span className="absolute left-0 top-1/2 h-px w-6 bg-border" />
                      </div>
                      <Badge
                        variant="secondary"
                        className="gap-1 font-normal shrink-0"
                      >
                        {icon}
                        {r.name}
                      </Badge>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {/* Absent rather than disabled when the whole topology fits. */}
          {branchPages > 1 && (
            <div className="mt-3">
              <DataTablePagination
                variant="compact"
                label={`connections on ${network.name}`}
                currentPage={branchPage}
                totalPages={branchPages}
                onPageChange={setTopologyPage}
              />
            </div>
          )}
        </CardContent>
      </Card>

      {/* General */}
      <Card>
        <CardHeader>
          <CardTitle>General</CardTitle>
        </CardHeader>
        <CardContent className="py-0">
          <Field label="Type">
            {formatLabel(network.type)}
          </Field>
          <Field label="Description">{network.description || EMPTY_VALUE}</Field>
          <Field label="ACLs">{config["security.acls"] || EMPTY_VALUE}</Field>
          <Field label="IPv4 Address">{config["ipv4.address"] || EMPTY_VALUE}</Field>
          <Field label="IPv6 Address">{config["ipv6.address"] || EMPTY_VALUE}</Field>
          <Field label="RX">
            {rx != null
              ? `${rx} (${counters.packets_received ?? 0} packets)`
              : EMPTY_VALUE}
          </Field>
          <Field label="TX">
            {tx != null
              ? `${tx} (${counters.packets_sent ?? 0} packets)`
              : EMPTY_VALUE}
          </Field>
          <Field label="Status">
            {network.status ? networkStatusDisplay(network.status).label : EMPTY_VALUE}
          </Field>
          <Field label="Managed">{network.managed ? "Yes" : "No"}</Field>
          <Field label="Used by Profiles">
            {profiles.length === 0 ? (
              "-"
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {profiles.map((r) =>
                  refBadge(r, <Layers className="h-3.5 w-3.5" />),
                )}
              </div>
            )}
          </Field>
        </CardContent>
      </Card>
    </div>
  );
}
