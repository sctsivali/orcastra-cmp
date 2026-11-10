"use client";

import * as React from "react";
import { DevicesPanel } from "@/components/used-by/panels/devices-panel";
import { NicAddressSection } from "./nic-address-section";
import { DetailSection } from "@/components/detail/detail-field";
import type { VMInfo } from "@/types/api";

// Two groups: `devices` is what is set on the instance itself, `expanded_devices` is what LXD
// applies after profile expansion. The split shows where a root disk size came from; an
// instance that overrides nothing has an empty first group and a full second one.
export function InstanceDevicesTab({ instance }: { instance: VMInfo }) {
  return (
    <div className="space-y-6">
      <NicAddressSection instance={instance} />
      <DetailSection
        title="Set on This Instance"
        description="Devices configured on the instance itself, which is where a root disk size chosen at create time lives."
      >
        <DevicesPanel devices={instance.devices ?? undefined} />
      </DetailSection>
      <DetailSection
        title="Effective"
        description="What the cluster applies after profile expansion. Read the effective root disk from here."
      >
        <DevicesPanel devices={instance.expanded_devices ?? undefined} />
      </DetailSection>
    </div>
  );
}
