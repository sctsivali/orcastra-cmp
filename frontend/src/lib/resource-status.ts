import type { BadgeTone } from "@/components/ui/badge";
import { formatLabel } from "@/lib/utils";

type ResourceStatusKey = "created" | "pending" | "errored" | "unknown";

export interface ResourceStatusDisplay {
  label: string;
  tone: BadgeTone;
}

const RESOURCE_STATUS_DISPLAY: Record<ResourceStatusKey, ResourceStatusDisplay> = {
  created: { label: "Created", tone: "positive" },
  pending: { label: "Pending", tone: "caution" },
  errored: { label: "Error", tone: "negative" },
  unknown: { label: "Unknown", tone: "neutral" },
};

function resourceStatusDisplay(status: string | null | undefined): ResourceStatusDisplay {
  const key = status?.trim().toLowerCase() as ResourceStatusKey | undefined;
  return key && RESOURCE_STATUS_DISPLAY[key]
    ? RESOURCE_STATUS_DISPLAY[key]
    : { label: formatLabel(status ?? "unknown"), tone: "neutral" };
}

export const networkStatusDisplay = resourceStatusDisplay;
export const storagePoolStatusDisplay = resourceStatusDisplay;
