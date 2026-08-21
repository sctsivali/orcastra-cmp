"use client";

import * as React from "react";
import { DevicesPanel } from "@/components/used-by/panels/devices-panel";
import { DetailSection } from "@/components/detail/detail-field";
import type { VMInfo } from "@/types/api";

// Two groups, because LXD means two different things by them and the difference decides where a
// root disk size actually came from. `devices` is what is set on the instance itself;
// `expanded_devices` is what LXD applies after profile expansion. An instance that overrides
// nothing has an empty first group and a full second one.
export function InstanceDevicesTab({ instance }: { instance: VMInfo }) {
  return (
    <div className="space-y-6">
      <DetailSection
        title="Set on this instance"
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
