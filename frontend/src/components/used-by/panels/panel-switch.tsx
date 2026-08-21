"use client";

import * as React from "react";
import { ConfigPanel } from "./config-panel";
import { DevicesPanel } from "./devices-panel";
import { CapacityPanel } from "./capacity-panel";
import { AclRulesPanel } from "./acl-rules-panel";
import type { SubjectPanel } from "../types";

export function PanelSwitch({ panel }: { panel: SubjectPanel }) {
  switch (panel.kind) {
    case "config":
      return <ConfigPanel entries={panel.entries} />;
    case "devices":
      return <DevicesPanel devices={panel.devices} />;
    case "capacity":
      return <CapacityPanel space={panel.space} inodes={panel.inodes} />;
    case "aclRules":
      return <AclRulesPanel ingress={panel.ingress} egress={panel.egress} />;
    case "custom":
      return <>{panel.render()}</>;
  }
}
