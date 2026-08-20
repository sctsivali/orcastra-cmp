"use client";

import { useState } from "react";
import { Badge } from "./ui/badge";
import { TableLink } from "./ui/table-link";
import { TableEmptyValue } from "./ui/table-text";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "./ui/dialog";
import { resourcesByProject } from "@/lib/used-by";

interface UsedByProjectsProps {
  usedBy: string[];
  resourceName: string;
}

export function UsedByProjects({ usedBy, resourceName }: UsedByProjectsProps) {
  const [isOpen, setIsOpen] = useState(false);
  // note: derived from the reference strings alone, so this no longer needs the instance
  // list and no longer fires a query from inside a table cell.
  const projectUsages = resourcesByProject(usedBy);
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
        {`${uniqueProjects} project${uniqueProjects !== 1 ? "s" : ""}`}
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
            {projectUsages.length === 0 ? (
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
                        {usage.resources.length} resource{usage.resources.length !== 1 ? "s" : ""}
                      </span>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {usage.resources.map((resource) => (
                      <Badge key={resource} variant="outline" className="text-xs px-2 py-1">
                        {resource}
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
