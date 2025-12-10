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
import { Users, Loader2, RotateCw } from "lucide-react";
import { GroupInfo, GroupListResponse } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { API_URL } from "@/lib/api";

export default function GroupsPage() {
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  useEffect(() => {
    fetchGroups();
  }, []);

  const fetchGroups = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_URL}/groups`);
      if (!response.ok) throw new Error("Failed to fetch groups");
      const data: GroupListResponse = await response.json();
      setGroups(data.groups);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to fetch groups",
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
            <Users className="h-8 w-8" />
            Groups
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage LXD and MicroCloud user groups and permissions
          </p>
        </div>
        <Button onClick={fetchGroups}>
          <RotateCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>All Groups ({groups.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {groups.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No groups found
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Identities</TableHead>
                  <TableHead>Permissions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groups.map((group) => (
                  <TableRow key={group.name}>
                    <TableCell className="font-medium">{group.name}</TableCell>
                    <TableCell>{group.description || "N/A"}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">
                        {group.identities?.length || 0} members
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">
                        {group.permissions?.length || 0} permissions
                      </Badge>
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
