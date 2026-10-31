"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Copy, ExternalLink, Info, XCircle } from "lucide-react";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { toast } from "@/hooks/use-toast";
import { copyText } from "@/lib/clipboard";
import type { TaskRecord } from "@/types/tasks";
import { cancelUnavailableReason, resourceHref } from "./task-format";

export interface TaskRowActionsProps {
  task: TaskRecord;
  onShow: (id: string) => void;
  onCancel: (task: TaskRecord) => void;
  /** Partner and above. A Tenant never sees the item, because the endpoint refuses before load. */
  canCancel: boolean;
  readOnly: boolean;
}

/** The kebab for a task row. Ids are addressed by specs; keep them stable. */
export function TaskRowActions({ task, onShow, onCancel, canCancel, readOnly }: TaskRowActionsProps) {
  const router = useRouter();
  const href = resourceHref(task);

  const copyId = React.useCallback(async () => {
    const ok = await copyText(task.id);
    toast(
      ok
        ? { title: "Copied", description: "Task id copied to clipboard." }
        : { title: "Could not copy task ID", description: "Select the id and copy it manually.", variant: "destructive" },
    );
  }, [task.id]);

  const actions: RowAction[] = [
    { id: "show-details", label: "Show Details", icon: Info, onSelect: () => onShow(task.id) },
    { id: "copy-id", label: "Copy Task ID", icon: Copy, onSelect: () => void copyId() },
    {
      id: "open-resource",
      label: "Open Resource",
      icon: ExternalLink,
      onSelect: () => {
        if (href) router.push(href);
      },
      unavailableReason: href ? undefined : "No resource to open",
    },
  ];

  if (canCancel) {
    actions.push({
      id: "cancel",
      label: "Cancel Task",
      icon: XCircle,
      destructive: true,
      separatorBefore: true,
      onSelect: () => onCancel(task),
      unavailableReason: cancelUnavailableReason(task, readOnly) ?? undefined,
    });
  }

  return <RowActions actions={actions} triggerLabel={`Actions for task ${task.id}`} />;
}
