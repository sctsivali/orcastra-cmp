"use client";

import { useState, useMemo } from "react";
import { useIsFetching, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertCircle,
  Loader2,
  Plus,
  Trash2,
  Users,
  Building2,
  WifiOff,
  Wifi,
  ShieldCheck,
} from "lucide-react";
import { api, integrationsAPI } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { PolicyManagementSection } from "@/components/policy-management-section";
import { TenantAccessDialog } from "@/components/tenant-access-dialog";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import type {
  UserGroupEntry,
  IntegrationOrganization,
  AuthentikSyncStatus,
} from "@/types";

// role badge styling
function roleBadge(role: string) {
  switch (role) {
    case "admin":
      return (
        <Badge variant="destructive" className="text-xs">
          Admin
        </Badge>
      );
    case "partner":
      return (
        <Badge className="bg-blue-600 hover:bg-blue-700 text-white text-xs">
          Partner
        </Badge>
      );
    case "tenant":
    default:
      return (
        <Badge variant="secondary" className="text-xs">
          Tenant
        </Badge>
      );
  }
}

export function UserGroupsSettings() {
  const queryClient = useQueryClient();
  const policiesFetching = useIsFetching({ queryKey: ["tenant-access-policies"] });
  const integrationOrgsFetching = useIsFetching({ queryKey: ["integration-organizations"] });
  const organizationsFetching = useIsFetching({ queryKey: ["organizations"] });
  const clustersFetching = useIsFetching({ queryKey: ["clusters"] });
  const [assignOpen, setAssignOpen] = useState(false);
  const [deleteEmail, setDeleteEmail] = useState<string | null>(null);
  const [accessEmail, setAccessEmail] = useState<string | null>(null);
  const [accessOrgIds, setAccessOrgIds] = useState<number[]>([]);

  // form state
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("tenant");
  const [orgId, setOrgId] = useState<string>("");

  // user table sorting
  const userSortAccessors = useMemo(
    () => ({
      email: (u: any) => u.user_email,
      role: (u: any) => u.role,
      organizations: (u: any) => u.organizations?.length ?? 0,
    }),
    [],
  );
  const {
    sortField: userSortField,
    sortDirection: userSortDirection,
    handleSort: handleUserSort,
    sortItems: sortUsers,
  } = useTableSort(userSortAccessors, "email");

  // fetch user list
  const {
    data: users,
    isLoading: usersLoading,
    isFetching: usersFetching,
    error: usersError,
    refetch: refetchUsers,
  } = useQuery({
    queryKey: ["user-groups"],
    queryFn: () => api.userGroups.list(),
  });

  // fetch organizations synced from external apps (OrcaHub)
  const { data: orgs } = useQuery({
    queryKey: ["integration-organizations"],
    queryFn: () => integrationsAPI.listOrganizations(),
  });

  // fetch identity provider sync status
  const { data: syncStatus, refetch: refetchSync, isFetching: syncStatusFetching } = useQuery({
    queryKey: ["user-groups-sync-status"],
    queryFn: () => api.userGroups.syncStatus(),
    refetchInterval: 30000, // check every 30s
  });

  // assign mutation
  const assignMutation = useMutation({
    mutationFn: api.userGroups.assign,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-groups"] });
      setAssignOpen(false);
      resetForm();
    },
  });

  // delete mutation
  const deleteMutation = useMutation({
    mutationFn: (userEmail: string) => api.userGroups.remove(userEmail),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-groups"] });
      setDeleteEmail(null);
    },
  });

  function resetForm() {
    setEmail("");
    setRole("tenant");
    setOrgId("");
  }

  // org is required for partner/tenant roles
  const orgRequired = role === "partner" || role === "tenant";
  const isRefreshingAll =
    usersFetching ||
    syncStatusFetching ||
    policiesFetching > 0 ||
    integrationOrgsFetching > 0 ||
    organizationsFetching > 0 ||
    clustersFetching > 0;

  // simple email regex (RFC 5322 simplified)
  const isValidEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);

  function handleAssign() {
    if (!email.trim()) return;
    if (!isValidEmail(email.trim())) return;
    if (orgRequired && !orgId) return;
    assignMutation.mutate({
      user_email: email.trim(),
      role,
      organization_id: orgId || undefined,
    });
  }

  async function handleRefreshAll() {
    try {
      await Promise.all([
        refetchSync(),
        refetchUsers(),
        queryClient.refetchQueries({ queryKey: ["tenant-access-policies"], type: "active" }),
        queryClient.refetchQueries({ queryKey: ["integration-organizations"], type: "active" }),
        queryClient.refetchQueries({ queryKey: ["organizations"], type: "active" }),
        queryClient.refetchQueries({ queryKey: ["clusters"], type: "active" }),
      ]);

      toast({
        title: "User access refreshed",
        description: "Sync status, policies, users, and organizations are up to date.",
      });
    } catch (error) {
      toast({
        title: "Refresh failed",
        description: error instanceof Error ? error.message : "Unable to refresh user access data.",
        variant: "destructive",
      });
    }
  }

  // helper: open access dialog for a tenant user
  const openAccessDialog = (user: UserGroupEntry) => {
    setAccessEmail(user.user_email);
    setAccessOrgIds(user.organizations?.map(o => parseInt(String(o.id))) || []);
  };

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
          disabled={isRefreshingAll}
          isRefreshing={isRefreshingAll}
          label="Refresh user groups, policies, and sync status"
          onClick={handleRefreshAll}
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
                  <Wifi className="h-4 w-4 text-green-500" />
                  <span className="text-sm text-green-600 dark:text-green-400">
                    {syncStatus.groups_exist
                      ? "Connected — role changes and groups sync automatically"
                      : "Connected — role changes sync automatically"}
                  </span>
                </>
              ) : (
                <>
                  <WifiOff className="h-4 w-4 text-yellow-500" />
                  <span className="text-sm text-yellow-600 dark:text-yellow-400">
                    Configured but unreachable — changes saved locally only
                  </span>
                </>
              )
            ) : (
              <>
                <WifiOff className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">
                  Not configured — set identity provider credentials to enable sync
                </span>
              </>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Access Policies Management */}
      <PolicyManagementSection showRefresh={false} />

      {/* User Groups Table */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>
                Assigned Users
              </CardTitle>
              <CardDescription className="mt-1.5">
                Assign roles and organizations to users by email. Changes sync
                to the identity provider when configured.
              </CardDescription>
            </div>
            <div className="flex gap-2">
              <Dialog open={assignOpen} onOpenChange={setAssignOpen}>
                <DialogTrigger asChild>
                  <Button size="sm" onClick={resetForm}>
                    <Plus className="h-4 w-4 mr-1.5" />
                    Assign User
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Assign User Role</DialogTitle>
                    <DialogDescription>
                      Assign a role and optionally an organization to a user by
                      their email address.
                    </DialogDescription>
                  </DialogHeader>

                  <div className="space-y-4 py-2">
                    <div className="space-y-2">
                      <Label htmlFor="user-email">Email Address</Label>
                      <Input
                        id="user-email"
                        type="email"
                        placeholder="user@example.com"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                      />
                      {email.trim() && !isValidEmail(email.trim()) && (
                        <p className="text-xs text-destructive">
                          Please enter a valid email address (e.g. user@example.com)
                        </p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <Label>Role</Label>
                      <Select value={role} onValueChange={setRole}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="admin">Admin</SelectItem>
                          <SelectItem value="partner">Partner</SelectItem>
                          <SelectItem value="tenant">Tenant</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Admin = full access, Partner = manage org &amp; clusters,
                        Tenant = view assigned resources
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label>
                        Organization{" "}
                        {orgRequired ? (
                          <span className="text-destructive font-normal">(required)</span>
                        ) : (
                          <span className="text-muted-foreground font-normal">(optional)</span>
                        )}
                      </Label>
                      <Select value={orgId || "__none__"} onValueChange={(v) => setOrgId(v === "__none__" ? "" : v)}>
                        <SelectTrigger>
                          <SelectValue placeholder="Select organization..." />
                        </SelectTrigger>
                        <SelectContent>
                          {!orgRequired && (
                            <SelectItem value="__none__">None</SelectItem>
                          )}
                          {orgs?.map((org: IntegrationOrganization) => (
                            <SelectItem key={org.id} value={String(org.id)}>
                              {org.name}
                              {org.app_name && (
                                <span className="text-muted-foreground ml-1">
                                  via {org.app_name}
                                </span>
                              )}
                            </SelectItem>
                          ))}
                          {(!orgs || orgs.length === 0) && orgRequired && (
                            <div className="px-2 py-1.5 text-xs text-muted-foreground">
                              No organizations synced — connect OrcaHub via Integrations first
                            </div>
                          )}
                        </SelectContent>
                      </Select>
                    </div>

                    {assignMutation.error && (
                      <div className="flex items-center gap-2 text-sm text-destructive">
                        <AlertCircle className="h-4 w-4" />
                        {(assignMutation.error as Error).message}
                      </div>
                    )}
                  </div>

                  <DialogFooter>
                    <Button
                      variant="outline"
                      onClick={() => setAssignOpen(false)}
                    >
                      Cancel
                    </Button>
                    <Button
                      onClick={handleAssign}
                      disabled={!email.trim() || !isValidEmail(email.trim()) || (orgRequired && !orgId) || assignMutation.isPending}
                    >
                      {assignMutation.isPending && (
                        <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                      )}
                      Assign
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {usersLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              <span className="ml-2 text-muted-foreground">Loading users...</span>
            </div>
          ) : usersError ? (
            <div className="flex items-center gap-2 py-8 justify-center text-destructive">
              <AlertCircle className="h-5 w-5" />
              <span>Failed to load users: {(usersError as Error).message}</span>
            </div>
          ) : !users?.length ? (
            <div className="text-center py-12 text-muted-foreground">
              <Users className="h-10 w-10 mx-auto mb-3 opacity-40" />
              <p className="text-sm">No users assigned yet</p>
              <p className="text-xs mt-1">
                Click &quot;Assign User&quot; to add a user with a role
              </p>
            </div>
          ) : (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead
                      label="Email"
                      field="email"
                      currentField={userSortField}
                      direction={userSortDirection}
                      onSort={handleUserSort}
                    />
                    <SortableTableHead
                      label="Role"
                      field="role"
                      currentField={userSortField}
                      direction={userSortDirection}
                      onSort={handleUserSort}
                    />
                    <SortableTableHead
                      label="Organizations"
                      field="organizations"
                      currentField={userSortField}
                      direction={userSortDirection}
                      onSort={handleUserSort}
                    />
                    <TableHead className="w-[120px]">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sortUsers(users).map((user: UserGroupEntry) => (
                    <TableRow key={user.user_email}>
                      <TableCell className="font-medium text-sm">
                        {user.user_email}
                      </TableCell>
                      <TableCell>{roleBadge(user.role)}</TableCell>
                      <TableCell>
                        {user.organizations?.length ? (
                          <div className="flex gap-1.5 flex-wrap">
                            {user.organizations.map((o) => (
                              <Badge
                                key={o.id}
                                variant="outline"
                                className="text-xs"
                              >
                                <Building2 className="h-3 w-3 mr-1" />
                                {o.name}
                              </Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          {/* Access button - only for Tenant users */}
                          {user.role === "tenant" && (
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-7 text-xs gap-1.5 text-blue-600 border-blue-200 hover:bg-blue-50 hover:text-blue-700 dark:border-blue-800 dark:hover:bg-blue-950"
                              onClick={() => openAccessDialog(user)}
                            >
                              <ShieldCheck className="h-3.5 w-3.5" />
                              Access
                            </Button>
                          )}
                          <Dialog
                            open={deleteEmail === user.user_email}
                            onOpenChange={(open) =>
                              setDeleteEmail(open ? user.user_email : null)
                            }
                          >
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <DialogTrigger asChild>
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    className="h-7 w-7 text-destructive hover:text-destructive"
                                  >
                                    <Trash2 className="h-3.5 w-3.5" />
                                  </Button>
                                </DialogTrigger>
                              </TooltipTrigger>
                              <TooltipContent side="top">
                                <p className="text-xs">Remove user</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                          <DialogContent>
                            <DialogHeader>
                              <DialogTitle>Remove User</DialogTitle>
                              <DialogDescription>
                                Remove <strong>{user.user_email}</strong> from
                                all organizations and identity provider groups?
                                This cannot be undone.
                              </DialogDescription>
                            </DialogHeader>
                            <DialogFooter>
                              <Button
                                variant="outline"
                                onClick={() => setDeleteEmail(null)}
                              >
                                Cancel
                              </Button>
                              <Button
                                variant="destructive"
                                onClick={() =>
                                  deleteMutation.mutate(user.user_email)
                                }
                                disabled={deleteMutation.isPending}
                              >
                                {deleteMutation.isPending && (
                                  <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                )}
                                Remove
                              </Button>
                            </DialogFooter>
                          </DialogContent>
                        </Dialog>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Tenant Access Dialog */}
      {accessEmail && (
        <TenantAccessDialog
          userEmail={accessEmail}
          userOrgIds={accessOrgIds}
          open={!!accessEmail}
          onOpenChange={(open) => {
            if (!open) {
              setAccessEmail(null);
              setAccessOrgIds([]);
            }
          }}
        />
      )}
    </div>
  );
}
