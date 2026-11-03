"use client";

import { Ban, Unplug } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { EmptyState } from "@/components/ui/empty-state";
import { useInstance } from "@/hooks/use-lxd";
import type { GuestTarget } from "@/lib/instance-files/api";

const AGENT_INSTALL = `modprobe 9pnet_virtio
mount -t 9p config /mnt -o access=0,transport=virtio || mount -t virtiofs config /mnt
cd /mnt
./install.sh
cd /
umount /mnt
reboot`;

type ConsoleTabOpener = (tab: "serial" | "graphics") => void;

interface WindowsGuestStateProps {
  title: string;
  description: string;
  onOpenConsoleTab?: ConsoleTabOpener;
}

/** A Windows guest: no agent LXD supports, so the Graphics console is the way in. */
export function WindowsGuestState({ title, description, onOpenConsoleTab }: WindowsGuestStateProps) {
  return (
    <EmptyState
      icon={Ban}
      title={title}
      description={description}
      action={
        onOpenConsoleTab ? (
          <Button size="sm" variant="outline" onClick={() => onOpenConsoleTab("graphics")}>Open Graphics</Button>
        ) : undefined
      }
    />
  );
}

export interface AgentMissingProps {
  target: GuestTarget;
  onRetry: () => void;
  onOpenConsoleTab?: ConsoleTabOpener;
  /** Shown when the guest turns out to be Windows. */
  windowsTitle: string;
  windowsDescription: string;
  /** The first paragraph: what needs the agent, and that it is not answering. */
  agentDescription: string;
}

/**
 * The guest agent of a virtual machine is not answering. Shared by the file explorer and the
 * Credentials panel, which reach the guest the same way, so both say the same thing about it and
 * offer the same install snippet.
 */
export function AgentMissing({
  target, onRetry, onOpenConsoleTab, windowsTitle, windowsDescription, agentDescription,
}: AgentMissingProps) {
  const { data } = useInstance(target.instanceName, target.host, true, target.project);
  const os = String(data?.config?.["image.os"] ?? "").toLowerCase();
  if (os.includes("windows")) {
    return <WindowsGuestState title={windowsTitle} description={windowsDescription} onOpenConsoleTab={onOpenConsoleTab} />;
  }
  return (
    <div className="mx-auto max-w-xl space-y-3 p-6 text-sm">
      <h3 className="flex items-center gap-2 text-base font-semibold">
        <Unplug className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        Guest Agent Not Reachable
      </h3>
      <p className="text-muted-foreground">{agentDescription}</p>
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
