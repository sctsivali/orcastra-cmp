"use client";

import { Info } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { InstanceGuestAgentStatus } from "@/types/api";

/**
 * Why a running virtual machine has no CPU, memory or process figures. Without the guest
 * agent (lxd-agent or incus-agent) the cluster sees the VM only from the host side, so
 * network and disk keep updating.
 */
export function GuestAgentNotice({ status }: { status: InstanceGuestAgentStatus | undefined }) {
  if (status !== "unreachable" && status !== "disabled") return null;
  return (
    <Alert>
      <Info aria-hidden="true" className="h-4 w-4" />
      <AlertTitle>
        {status === "disabled" ? "Guest metrics are turned off" : "Guest agent not reachable"}
      </AlertTitle>
      <AlertDescription>
        {status === "disabled"
          ? "security.agent.metrics is false on this instance, so the cluster does not ask the guest for vCPU, memory or processes."
          : "The cluster cannot read vCPU, memory or processes from inside this virtual machine. Check that the guest agent (lxd-agent on LXD, incus-agent on Incus) is running in the guest."}{" "}
        Network and disk figures still come from the host.
      </AlertDescription>
    </Alert>
  );
}
