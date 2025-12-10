"use client";

import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { User, Loader2, RotateCw, UserCircle } from "lucide-react";
import { IdentityInfo, IdentityListResponse } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { API_URL } from "@/lib/api";

export default function IdentitiesPage() {
  const [identities, setIdentities] = useState<IdentityInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  useEffect(() => {
    fetchIdentities();
  }, []);

  const fetchIdentities = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_URL}/identities`);
      if (!response.ok) throw new Error("Failed to fetch identities");
      const data: IdentityListResponse = await response.json();
      setIdentities(data.identities);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to fetch identities",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <UserCircle className="h-8 w-8" />
            Identities
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage LXD and MicroCloud user identities
          </p>
        </div>
        <Button onClick={fetchIdentities}>
          <RotateCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>All Identities ({identities.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {identities.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No identities found
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Groups</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {identities.map((identity) => (
                  <TableRow key={identity.id}>
                    <TableCell className="font-mono text-xs">
                      {identity.id}
                    </TableCell>
                    <TableCell className="font-medium">
                      {identity.name || "N/A"}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{identity.type}</Badge>
                    </TableCell>
                    <TableCell>
                      {identity.groups.map((group) => (
                        <Badge key={group} variant="secondary" className="mr-1">
                          {group}
                        </Badge>
                      ))}
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
