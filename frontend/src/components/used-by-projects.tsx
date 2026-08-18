"use client";

import { useState, useEffect, useMemo } from "react";
import { Badge } from "./ui/badge";
import { TableLink } from "./ui/table-link";
import { TableEmptyValue } from "./ui/table-text";
import { LoadingLottieSmall } from "./loading-lottie";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "./ui/dialog";
import { useInstances } from "@/hooks/use-lxd";

interface UsedByProjectsProps {
  usedBy: string[];
  resourceName: string;
}

interface ProjectUsage {
  project: string;
  instances: string[];
}

export function UsedByProjects({ usedBy, resourceName }: UsedByProjectsProps) {
  const [isOpen, setIsOpen] = useState(false);
  const { data, isLoading } = useInstances();
  const instances = (data as any)?.vms || [];
  
  const projectUsages = useMemo(() => {
    // Build mapping: instance name -> project
    const instanceProjectMap = new Map<string, string>();
    instances.forEach((instance: any) => {
      instanceProjectMap.set(instance.name, instance.project || 'default');
    });
    
    // Extract instance names from used_by URLs and group by project
    const projectMap = new Map<string, Set<string>>();
    
    usedBy.forEach((url) => {
      // Extract resource name from URL like "/1.0/instances/instance-name"
      const match = url.match(/\/1\.0\/instances\/([^/?]+)/);
      if (match) {
        const instanceName = match[1];
        const project = instanceProjectMap.get(instanceName) || 'default';
        
        if (!projectMap.has(project)) {
          projectMap.set(project, new Set());
        }
        projectMap.get(project)!.add(instanceName);
      }
    });
    
    // Convert to array format
    return Array.from(projectMap.entries()).map(([project, instances]) => ({
      project,
      instances: Array.from(instances).sort(),
    })).sort((a, b) => a.project.localeCompare(b.project));
  }, [instances, usedBy]);

  const uniqueProjects = projectUsages.length;

  if (usedBy.length === 0) {
    return <TableEmptyValue label="No projects" />;
  }

  return (
    <>
      <TableLink
        onClick={() => setIsOpen(true)}
        aria-label={`Show projects using ${resourceName}`}
      >
        {`${uniqueProjects || usedBy.length} project${(uniqueProjects || usedBy.length) !== 1 ? "s" : ""}`}
      </TableLink>

      <Dialog open={isOpen} onOpenChange={setIsOpen}>
        <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader className="pr-8">
            <DialogTitle>Projects Using {resourceName}</DialogTitle>
            <DialogDescription>
              {usedBy.length} resource(s) across {uniqueProjects} project(s)
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 mt-4">
            {isLoading ? (
              <div className="flex justify-center py-4">
                <LoadingLottieSmall />
              </div>
            ) : projectUsages.length === 0 ? (
              <div className="text-center text-muted-foreground py-4">
                No project information available
              </div>
            ) : (
              projectUsages.map((usage) => (
                <div key={usage.project} className="border rounded-lg p-4 bg-muted/30">
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <Badge variant="default" className="text-sm px-3 py-1">
                        {usage.project}
                      </Badge>
                      <span className="text-sm text-muted-foreground">
                        {usage.instances.length} instance{usage.instances.length !== 1 ? 's' : ''}
                      </span>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {usage.instances.map((instance) => (
                      <Badge key={instance} variant="outline" className="text-xs px-2 py-1">
                        {instance}
                      </Badge>
                    ))}
                  </div>
                </div>
              ))
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
