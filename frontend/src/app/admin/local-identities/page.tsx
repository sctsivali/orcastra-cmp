"use client";

/**
 * Local Identities (mini deployment)
 *
 * Admin surface for client-certificate authentication: issue a cert for a user,
 * change roles, revoke, or delete. Replaces Authentik's user/group directory when
 * AUTH_MODE=client-cert. Admin-only (gated in middleware and by the backend).
 */

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { CopyableId } from "@/components/ui/copyable-id";
import { formatLabel } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { RefreshButton } from "@/components/ui/refresh-button";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { useTableSort } from "@/hooks/use-table-sort";
import { useToast } from "@/hooks/use-toast";
import { localIdentitiesAPI, type LocalIdentity } from "@/lib/api";
import { UserPlus, Trash2, ShieldCheck, ShieldOff, Copy, Users } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";

// Username/label shown in the table - never the FQDN cert CN (which looks like a domain).
function identityName(id: LocalIdentity): string {
  return id.display_name || id.common_name.split(".")[0];
}

type IdentitySortField = "name" | "role" | "status" | "fingerprint" | "expires";
const IDENTITY_ACCESSORS: Record<IdentitySortField, (i: LocalIdentity) => unknown> = {
  name: (i) => identityName(i),
  role: (i) => i.role,
  status: (i) => i.status,
  fingerprint: (i) => i.fingerprint,
  expires: (i) => (i.not_after ? new Date(i.not_after).getTime() : null),
};

const ROLES = ["tenant", "partner", "admin"];

export default function LocalIdentitiesPage() {
  const { toast } = useToast();
  const qc = useQueryClient();

  const { data: identities = [], isLoading, refetch, isFetching } = useQuery({
    queryKey: ["local-identities"],
    queryFn: localIdentitiesAPI.list,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    IDENTITY_ACCESSORS, "name", "asc",
  );
  // Issue dialog state
  const [issueOpen, setIssueOpen] = useState(false);
  const [commonName, setCommonName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("tenant");
  const [ttlDays, setTtlDays] = useState<string>("");
  const [issuedPassword, setIssuedPassword] = useState<string | null>(null);

  const issueMutation = useMutation({
    mutationFn: () =>
      localIdentitiesAPI.issue({
        common_name: commonName.trim(),
        display_name: displayName.trim() || undefined,
        role,
        ttl_days: ttlDays ? Number(ttlDays) : undefined,
      }),
    onSuccess: ({ blob, password, filename }) => {
      // Trigger the one-time .p12 download.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      setIssuedPassword(password);
      setCommonName("");
      setDisplayName("");
      setTtlDays("");
      qc.invalidateQueries({ queryKey: ["local-identities"] });
      toast({ title: "Certificate issued", description: "The .p12 download has started." });
    },
    onError: (e: any) =>
      toast({ title: "Issue failed", description: e.message, variant: "destructive" }),
  });

  const updateMutation = useMutation({
    mutationFn: ({ fp, payload }: { fp: string; payload: { role?: string; status?: string } }) =>
      localIdentitiesAPI.update(fp, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["local-identities"] });
      toast({ title: "Identity updated" });
    },
    onError: (e: any) =>
      toast({ title: "Update failed", description: e.message, variant: "destructive" }),
  });

  const deleteMutation = useMutation({
    mutationFn: (fp: string) => localIdentitiesAPI.remove(fp),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["local-identities"] });
      toast({ title: "Identity deleted" });
    },
    onError: (e: any) =>
      toast({ title: "Delete failed", description: e.message, variant: "destructive" }),
  });

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">Local Identities</h1>
          <p className="text-muted-foreground mt-2">
            Client-certificate users for this single-instance deployment.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton onClick={() => refetch()} isRefreshing={isFetching} />
          <Dialog open={issueOpen} onOpenChange={setIssueOpen}>
            <DialogTrigger asChild>
              <Button>
                <UserPlus className="mr-2 h-4 w-4" /> Issue Identity
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Issue a client certificate</DialogTitle>
                <DialogDescription>
                  Generates a password-protected .p12 the user imports into their browser.
                  The password is shown once after issuing.
                </DialogDescription>
              </DialogHeader>
              {issuedPassword ? (
                <div className="space-y-3">
                  <p className="text-sm">
                    The .p12 file has downloaded. Share this one-time password with the user
                    over a separate channel:
                  </p>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 rounded bg-muted px-3 py-2 text-sm break-all">
                      {issuedPassword}
                    </code>
                    <Button
                      variant="outline"
                      size="icon"
                      onClick={() => {
                        navigator.clipboard.writeText(issuedPassword);
                        toast({ title: "Password copied" });
                      }}
                    >
                      <Copy className="h-4 w-4" />
                    </Button>
                  </div>
                  <DialogFooter>
                    <Button onClick={() => { setIssuedPassword(null); setIssueOpen(false); }}>
                      Done
                    </Button>
                  </DialogFooter>
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="cn">Username / label</Label>
                    <Input id="cn" value={commonName} onChange={(e) => setCommonName(e.target.value)} placeholder="jane.doe" />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="dn">Display name (optional)</Label>
                    <Input id="dn" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Jane Doe" />
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-2">
                      <Label>Role</Label>
                      <Select value={role} onValueChange={setRole}>
                        <SelectTrigger><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {ROLES.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="ttl">Validity (days)</Label>
                      <Input id="ttl" type="number" value={ttlDays} onChange={(e) => setTtlDays(e.target.value)} placeholder="365" />
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={() => issueMutation.mutate()}
                      disabled={!commonName.trim() || issueMutation.isPending}
                    >
                      {issueMutation.isPending ? "Issuing..." : "Issue"}
                    </Button>
                  </DialogFooter>
                </div>
              )}
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Enrolled identities ({identities.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : identities.length === 0 ? (
            <EmptyState icon={Users} title="No identities yet" description="Issue one to get started." />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Role" field="role" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Fingerprint" field="fingerprint" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <SortableTableHead label="Expires" field="expires" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortItems(identities).map((id: LocalIdentity) => (
                  <TableRow key={id.fingerprint}>
                    <TableCell>
                      <div className="font-medium">{identityName(id)}</div>
                    </TableCell>
                    <TableCell>
                      <Select
                        value={id.role}
                        onValueChange={(v) => updateMutation.mutate({ fp: id.fingerprint, payload: { role: v } })}
                      >
                        <SelectTrigger className="w-28 h-8"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {ROLES.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatLabel(id.status)}</TableCell>
                    <TableCell>
                      <CopyableId
                        value={id.fingerprint}
                        head={16}
                        tail={0}
                        label="Fingerprint"
                        className="text-muted-foreground"
                      />
                    </TableCell>
                    <TableCell
                      className="text-sm text-muted-foreground"
                      title={id.not_after ? new Date(id.not_after).toLocaleString() : undefined}
                    >
                      {id.not_after ? new Date(id.not_after).toLocaleDateString() : "-"}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        {id.status === "active" ? (
                          <Button
                            variant="ghost" size="icon" title="Revoke"
                            onClick={() => updateMutation.mutate({ fp: id.fingerprint, payload: { status: "revoked" } })}
                          >
                            <ShieldOff className="h-4 w-4" />
                          </Button>
                        ) : (
                          <Button
                            variant="ghost" size="icon" title="Re-activate"
                            onClick={() => updateMutation.mutate({ fp: id.fingerprint, payload: { status: "active" } })}
                          >
                            <ShieldCheck className="h-4 w-4" />
                          </Button>
                        )}
                        <AlertDialog>
                          <AlertDialogTrigger asChild>
                            <Button variant="ghost" size="icon" title="Delete">
                              <Trash2 className="h-4 w-4 text-destructive" />
                            </Button>
                          </AlertDialogTrigger>
                          <AlertDialogContent>
                            <AlertDialogHeader>
                              <AlertDialogTitle>Delete identity?</AlertDialogTitle>
                              <AlertDialogDescription>
                                This permanently removes {identityName(id)}. Prefer revoke to keep an audit trail.
                              </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                              <AlertDialogCancel>Cancel</AlertDialogCancel>
                              <AlertDialogAction onClick={() => deleteMutation.mutate(id.fingerprint)}>
                                Delete
                              </AlertDialogAction>
                            </AlertDialogFooter>
                          </AlertDialogContent>
                        </AlertDialog>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
