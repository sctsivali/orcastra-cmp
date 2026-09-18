"use client";

import * as React from "react";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TableLink } from "@/components/ui/table-link";
import { UsagePanel } from "./usage-panel";
import { SubjectSummary } from "./subject-summary";
import { PanelSwitch } from "./panels/panel-switch";
import { projectsFromUsedBy } from "@/lib/used-by";
import { USAGE_PANEL_ID, type UsedBySubject } from "./types";
import type { UsedByDialogState } from "./use-used-by-dialog";
import { inventoryLinks } from "@/lib/inventory-links";

// One dialog for the whole used_by family, opened from either the "Used By" or the "Projects"
// cell of six inventory tables.
//
// It replaces two components that rendered the same references with two different parsers and
// each dropped half the answer: one showed project and name with no kind, the other kind and
// name with no project, and the second one re-parsed with a weaker regex than the module whose
// header says it must stay in step with the backend's tenant-visibility parser.
//
// note: the two table columns stay separate. They carry different sort and facet accessors and
// different URL keys, so merging the cells would break two facets and a shared link. Only the
// dialog merges; the columns just open it in different views.
export function UsedByDialog({
  subject,
  panelId,
  view,
  open,
  onOpenChange,
  setPanelId,
  setView,
}: UsedByDialogState) {
  const [announcement, setAnnouncement] = React.useState("");

  React.useEffect(() => {
    if (!open) setAnnouncement("");
  }, [open]);

  if (!subject) return null;

  const panels = subject.panels ?? [];
  // important: through the shared parser, not a local regex. Writing a second one here is
  // exactly the defect this component exists to remove, and "it is only for a count in the
  // subtitle" is the same reasoning that produced the one being deleted. It is also cached on
  // the array identity, so this costs nothing the table has not already paid.
  const projectCount = projectsFromUsedBy(subject.usedBy).length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="pr-8 pb-4">
          <DialogTitle>{titleFor(subject)}</DialogTitle>
          <DialogDescription>
            {`${subject.usedBy.length} reference${subject.usedBy.length === 1 ? "" : "s"} across ` +
              `${projectCount} project${projectCount === 1 ? "" : "s"}`}
            {subject.description ? ` · ${subject.description}` : ""}
          </DialogDescription>
        </DialogHeader>

        <SubjectSummary facts={subject.facts} />

        <DialogBody className="pr-2">
          {panels.length === 0 ? (
            <UsagePanel
              usedBy={subject.usedBy}
              node={subject.node}
              view={view}
              onViewChange={setView}
              onCopied={setAnnouncement}
            />
          ) : (
            <Tabs value={panelId} onValueChange={setPanelId}>
              {/* A single-panel subject gets no tab strip: one tab is chrome that costs a row of
                  vertical density and teaches nothing. */}
              <TabsList>
                <TabsTrigger value={USAGE_PANEL_ID}>Usage</TabsTrigger>
                {panels.map((panel) => (
                  <TabsTrigger key={panel.id} value={panel.id}>
                    {panel.label}
                  </TabsTrigger>
                ))}
              </TabsList>

              <TabsContent value={USAGE_PANEL_ID} className="mt-4">
                <UsagePanel
                  usedBy={subject.usedBy}
                  node={subject.node}
                  view={view}
                  onViewChange={setView}
                  onCopied={setAnnouncement}
                />
              </TabsContent>
              {panels.map((panel) => (
                <TabsContent key={panel.id} value={panel.id} className="mt-4">
                  <PanelSwitch panel={panel} />
                </TabsContent>
              ))}
            </Tabs>
          )}
        </DialogBody>

        <span aria-live="polite" className="sr-only">
          {announcement}
        </span>
      </DialogContent>
    </Dialog>
  );
}

function titleFor(subject: UsedBySubject): React.ReactNode {
  // Networks are the one kind with a real detail route, so the title links there. Do not fake
  // this for the others: every other destination is a filtered list, not a record page.
  if (subject.kind === "networks" && subject.node) {
    return (
      <TableLink
        href={inventoryLinks.networkDetail(subject.name, subject.node, subject.project)}
        title={`Open the full detail page for ${subject.name}`}
      >
        {subject.name}
      </TableLink>
    );
  }
  return subject.name;
}
