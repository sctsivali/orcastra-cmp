import type { GpuClass } from "@/lib/gpu";

export type InstanceType = "container" | "vm";

export interface InstanceCreateFormState {
  type: InstanceType;
  name: string;
  description: string;
  selectedNode: string;
  selectedProject: string;
  sourceType: "image" | "none";
  selectedImage: string;
  imageServer: string;
  imageLabel: string;
  selectedIso: string;
  selectedIsoPool: string;
  selectedIsoLabel: string;
  selectedPool: string;
  cpu: string;
  memoryValue: string;
  memoryUnit: string;
  memorySwap: string;
  diskPriority: string;
  maxProcesses: string;
  storageValue: string;
  storageUnit: string;
  tags: string[];
}

export interface GpuDevice {
  id: string;
  name: string;
  identifyBy: "pci" | "id";
  pci: string;
  gid: string;
  label?: string;
  gpuClass?: GpuClass;
}

export const MEMORY_UNITS = ["B", "kB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB"];
export const STORAGE_UNITS = [
  "kB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB", "PB", "PiB", "EB", "EiB",
];

export const ADVANCED_DEFAULTS = {
  secProtectDelete: "",
  secPrivileged: "",
  secNesting: "",
  secProtectShift: "",
  secBaseHostId: "",
  secIdmapSize: "",
  secUniqueIdmap: "",
  // LXD releases disagree on this key. The backend omits it for daemon versions that reject it.
  secSecureboot: "false",
  secCsm: "",
  migStateful: "",
  migEvacuate: "",
  bootAutostart: "",
  bootAutostartDelay: "",
  bootAutostartPriority: "",
  bootHostShutdownTimeout: "",
  bootStopPriority: "",
  ciUser: "",
  ciVendor: "",
  ciNetwork: "",
};

export type AdvancedKey = keyof typeof ADVANCED_DEFAULTS;

export const DEFAULT_INSTANCE_CREATE_FORM: InstanceCreateFormState = {
  type: "container",
  name: "",
  description: "",
  selectedNode: "",
  selectedProject: "default",
  sourceType: "image",
  selectedImage: "",
  imageServer: "",
  imageLabel: "",
  selectedIso: "",
  selectedIsoPool: "",
  selectedIsoLabel: "",
  selectedPool: "",
  cpu: "1",
  memoryValue: "1",
  memoryUnit: "GiB",
  memorySwap: "true",
  diskPriority: "5",
  maxProcesses: "",
  storageValue: "10",
  storageUnit: "GiB",
  tags: [],
};

export function isPositiveInteger(value: string): boolean {
  return /^[1-9]\d*$/.test(value.trim());
}
