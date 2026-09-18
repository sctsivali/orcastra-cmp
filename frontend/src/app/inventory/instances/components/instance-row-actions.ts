import { Copy, Monitor, Play, RefreshCw, RotateCw, Square, Terminal, Trash2 } from "lucide-react";
import type { RowAction } from "@/components/ui/row-actions";
import type { VMInfo } from "@/types/api";
import { actionKey, type InstanceAction } from "./use-instance-actions";

export type InstancePermission = "view" | "operate" | "console";

/** The wording every read-only refusal in a row menu uses; specs match on it. */
export const READ_ONLY_REASON = "Disabled in read-only mode";

const IN_PROGRESS: Record<InstanceAction | "delete", string> = {
  start: "Start in progress",
  stop: "Stop in progress",
  restart: "Restart in progress",
  delete: "Delete in progress",
};

export interface InstanceRowActionsInput {
  instance: Pick<VMInfo, "name" | "host" | "project" | "status" | "type">;
  isReadOnly: boolean;
  /** Partner and above. The backend refuses copy, rebuild and delete below that. */
  canManage: boolean;
  /** Keys of the lifecycle actions in flight (actionKey), from useInstanceActions. */
  pendingActions: ReadonlySet<string>;
  deletePending: boolean;
  /** Undefined when allowed; otherwise the reason shown under the item. */
  permissionReason: (permission: InstancePermission) => string | undefined;
  onStart: () => void;
  onStop: () => void;
  onRestart: () => void;
  onTerminal: () => void;
  onConsole: () => void;
  onCopy: () => void;
  onRebuild: () => void;
  onDelete: () => void;
}

/**
 * The row menu for one instance: lifecycle, then access, then management, then delete.
 *
 * Items are offered by state (Start only when not Running; Stop, Restart, Terminal and Console
 * only when Running) and by role (Copy, Rebuild and Delete only for canManage), the same gating
 * the inline buttons had. What changed is that a refusal is a reason under the label rather than
 * a greyed button: pending beats read-only, which beats the permission answer.
 */
export function instanceRowActions(input: InstanceRowActionsInput): RowAction[] {
  const { instance, isReadOnly, canManage, pendingActions, deletePending, permissionReason } = input;
  const project = instance.project || "default";
  const running = instance.status === "Running";
  const readOnly = isReadOnly ? READ_ONLY_REASON : undefined;

  const lifecycle = (action: InstanceAction): Pick<RowAction, "pending" | "unavailableReason"> => {
    const pending = pendingActions.has(actionKey(instance.host, project, instance.name, action));
    return {
      pending,
      unavailableReason: pending ? IN_PROGRESS[action] : readOnly ?? permissionReason("operate"),
    };
  };
  const manage = readOnly ?? permissionReason("operate");
  const consoleReason = permissionReason("console");

  const actions: RowAction[] = [];

  if (!running) {
    actions.push({ id: "start", label: "Start Instance", icon: Play, onSelect: input.onStart, ...lifecycle("start") });
  } else {
    actions.push(
      { id: "stop", label: "Stop Instance", icon: Square, onSelect: input.onStop, ...lifecycle("stop") },
      { id: "restart", label: "Restart Instance", icon: RotateCw, onSelect: input.onRestart, ...lifecycle("restart") },
      // Opening a terminal or console is a read, so read-only mode does not withhold it.
      { id: "terminal", label: "Open Terminal", icon: Terminal, onSelect: input.onTerminal, separatorBefore: true, unavailableReason: consoleReason },
      { id: "console", label: "Open Console", icon: Monitor, onSelect: input.onConsole, unavailableReason: consoleReason },
    );
  }

  if (canManage) {
    actions.push(
      { id: "copy", label: "Copy Instance", icon: Copy, onSelect: input.onCopy, separatorBefore: true, unavailableReason: manage },
      { id: "rebuild", label: "Rebuild Instance", icon: RefreshCw, onSelect: input.onRebuild, unavailableReason: manage },
      {
        id: "delete",
        label: "Delete Instance",
        icon: Trash2,
        destructive: true,
        separatorBefore: true,
        onSelect: input.onDelete,
        pending: deletePending,
        unavailableReason: deletePending ? IN_PROGRESS.delete : readOnly,
      },
    );
  }

  return actions;
}
