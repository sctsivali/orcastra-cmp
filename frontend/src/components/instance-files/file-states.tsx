"use client";

import { AlertTriangle, Ban, Cloud, Clock, FolderX, HardDrive, Lock, Pause, Power, ShieldAlert, Unplug } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { EmptyState } from "@/components/ui/empty-state";
import { useInstance } from "@/hooks/use-lxd";
import { fileErrorCode, type GuestTarget } from "@/lib/instance-files/api";
import { describeReadError } from "@/lib/read-error";

const AGENT_INSTALL = `modprobe 9pnet_virtio
mount -t 9p config /mnt -o access=0,transport=virtio || mount -t virtiofs config /mnt
cd /mnt
./install.sh
cd /
umount /mnt
reboot`;

interface StateProps {
  target: GuestTarget;
  error: unknown;
  path: string;
  onRetry: () => void;
  onUp: () => void;
  onRequestAccess: () => void;
  onOpenConsoleTab?: (tab: "serial" | "graphics") => void;
}

function AgentMissing({ target, onRetry, onOpenConsoleTab }: Pick<StateProps, "target" | "onRetry" | "onOpenConsoleTab">) {
  const { data } = useInstance(target.instanceName, target.host, true, target.project);
  const os = String(data?.config?.["image.os"] ?? "").toLowerCase();
  if (os.includes("windows")) {
    return (
      <EmptyState
        icon={Ban}
        title="Files are not available for this Windows guest"
        description="File access goes through a guest agent, and LXD supports that agent only on Linux with systemd. Use the Graphics tab to work in the guest."
        action={
          onOpenConsoleTab ? (
            <Button size="sm" variant="outline" onClick={() => onOpenConsoleTab("graphics")}>Open Graphics</Button>
          ) : undefined
        }
      />
    );
  }
  return (
    <div className="mx-auto max-w-xl space-y-3 p-6 text-sm">
      <h3 className="flex items-center gap-2 text-base font-semibold">
        <Unplug className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        Guest Agent Not Reachable
      </h3>
      <p className="text-muted-foreground">
        File access inside a virtual machine goes through the guest agent, and it is not answering. If this VM has just started, give it a minute.
      </p>
      <p className="text-muted-foreground">
        To install the agent once, sign in as root on the Serial console and run these commands. The last one restarts the guest. Official images already include the agent, and it runs only on Linux with systemd.
      </p>
      <CodeBlock value={AGENT_INSTALL} />
      <div className="flex gap-2">
        {onOpenConsoleTab && (
          <Button size="sm" variant="outline" onClick={() => onOpenConsoleTab("serial")}>Open Serial</Button>
        )}
        <Button size="sm" onClick={onRetry}>Check again</Button>
      </div>
    </div>
  );
}

/** Every refusal a listing can meet, in words that say what to do next. */
export function FileStates({ target, error, path, onRetry, onUp, onRequestAccess, onOpenConsoleTab }: StateProps) {
  const code = fileErrorCode(error);
  const retry = <Button size="sm" variant="outline" onClick={onRetry}>Retry</Button>;
  const up = path === "/" ? undefined : <Button size="sm" variant="outline" onClick={onUp}>Go up</Button>;
  switch (code) {
    case "agent_unavailable":
      return <AgentMissing target={target} onRetry={onRetry} onOpenConsoleTab={onOpenConsoleTab} />;
    case "permission_denied":
      return <EmptyState icon={Lock} title="You cannot open this folder" description="The guest refused access to this path." action={up} />;
    case "not_found":
      return <EmptyState icon={FolderX} title="This folder no longer exists" description="It may have been moved or deleted." action={up} />;
    case "not_a_directory":
      return <EmptyState icon={FolderX} title="This path is not a folder" action={up} />;
    case "instance_not_running":
      return <EmptyState icon={Power} title="This instance is stopped" description="Start it to browse its files, then check again." action={retry} />;
    case "instance_frozen":
      return <EmptyState icon={Pause} title="This instance is frozen" description="Resume it to browse its files, then check again." action={retry} />;
    case "instance_not_found":
      return <EmptyState icon={FolderX} title="This instance no longer exists" />;
    case "cluster_unreachable":
      return <EmptyState icon={Cloud} title="Cluster not reachable" description={`${target.host} did not answer. Nothing changed.`} action={retry} />;
    case "cluster_not_pinned":
      return <EmptyState icon={ShieldAlert} title="Cluster certificate not confirmed" description="This cluster's certificate could not be confirmed, so nothing was sent." action={retry} />;
    case "guest_timeout":
    case "guest_disconnected":
      return <EmptyState icon={Clock} title="The guest did not answer" description="It may be busy or still starting. Nothing changed." action={retry} />;
    case "migration_fenced":
      return <EmptyState icon={HardDrive} title="This instance is being migrated" description="Files are unavailable until the move finishes." action={retry} />;
    case "feature_disabled":
      return <EmptyState icon={Ban} title="File access is turned off" description="This deployment does not offer file access." />;
    case "break_glass_required":
      return (
        <EmptyState
          icon={ShieldAlert}
          title="This instance belongs to another organization"
          description="Reaching its files needs a recorded break-glass grant, the same as opening a shell."
          action={<Button size="sm" onClick={onRequestAccess}>Request access</Button>}
        />
      );
    default: {
      if ((error as { status?: number } | null)?.status === 403) {
        return <EmptyState icon={Lock} title="Your access does not include files" description="Your access to this instance does not include file transfer." />;
      }
      return (
        <EmptyState
          icon={AlertTriangle}
          title="Could not load this folder"
          description={`The folder could not be read: ${describeReadError(error).reason}. This says nothing about whether it exists.`}
          action={retry}
        />
      );
    }
  }
}
