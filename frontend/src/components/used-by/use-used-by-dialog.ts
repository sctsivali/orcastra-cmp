"use client";

import * as React from "react";
import { USAGE_PANEL_ID, type UsedBySubject, type UsageView } from "./types";

// One dialog per page, driven by whichever cell was clicked.
//
// important: this exists so a page mounts ONE Radix Dialog root instead of one per cell. The
// six pages this replaces mounted two per row, so a 25-row page carried fifty. The dialog is
// hoisted next to the table and the cells only set state.
export interface UsedByDialogState {
  subject: UsedBySubject | null;
  panelId: string;
  view: UsageView;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  setPanelId: (id: string) => void;
  setView: (view: UsageView) => void;
}

export interface UsedByDialogController {
  /** The "Used By" cell: the references as a flat list. */
  openUsage: (subject: UsedBySubject) => void;
  /** The "Projects" cell: the same references, grouped by project. */
  openProjects: (subject: UsedBySubject) => void;
  /** A cell that maps to one of the subject's other panels, ex; devices or ACL rules. */
  openPanel: (subject: UsedBySubject, panelId: string) => void;
  close: () => void;
  dialogProps: UsedByDialogState;
}

export function useUsedByDialog(): UsedByDialogController {
  const [subject, setSubject] = React.useState<UsedBySubject | null>(null);
  const [panelId, setPanelId] = React.useState<string>(USAGE_PANEL_ID);
  const [view, setView] = React.useState<UsageView>("flat");
  const [open, setOpen] = React.useState(false);

  const show = React.useCallback(
    (next: UsedBySubject, panel: string, nextView: UsageView) => {
      setSubject(next);
      setPanelId(panel);
      setView(nextView);
      setOpen(true);
    },
    [],
  );

  return React.useMemo<UsedByDialogController>(
    () => ({
      openUsage: (next) => show(next, USAGE_PANEL_ID, "flat"),
      openProjects: (next) => show(next, USAGE_PANEL_ID, "grouped"),
      openPanel: (next, id) => show(next, id, "flat"),
      close: () => setOpen(false),
      dialogProps: {
        subject,
        panelId,
        view,
        open,
        onOpenChange: setOpen,
        setPanelId,
        setView,
      },
    }),
    [show, subject, panelId, view, open],
  );
}
