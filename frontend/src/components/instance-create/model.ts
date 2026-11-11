import { DEFAULT_BYTE_UNITS, defaultFormUnit, type ByteUnitSystem } from "@/lib/byte-units";
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
  memoryUnit: "GB",
  memorySwap: "true",
  diskPriority: "5",
  maxProcesses: "",
  storageValue: "10",
  storageUnit: "GB",
  tags: [],
};

// The unit each system starts a size field in.
const STARTING_UNITS: string[] = [defaultFormUnit("decimal"), defaultFormUnit("binary")];

/**
 * Size fields still at their untouched defaults move to the account's units, which can arrive after
 * the form (a missing or stale hint cookie). A field the operator typed in keeps what it has.
 * Returns the same object when nothing moves, so it is safe in a state updater.
 */
export function followAccountUnits(form: InstanceCreateFormState, units: ByteUnitSystem): InstanceCreateFormState {
  const unit = defaultFormUnit(units);
  const untouched = (value: string, current: string, initial: string) =>
    value === initial && current !== unit && STARTING_UNITS.includes(current);
  const memory = untouched(form.memoryValue, form.memoryUnit, DEFAULT_INSTANCE_CREATE_FORM.memoryValue);
  const storage = untouched(form.storageValue, form.storageUnit, DEFAULT_INSTANCE_CREATE_FORM.storageValue);
  if (!memory && !storage) return form;
  return {
    ...form,
    ...(memory ? { memoryUnit: unit } : {}),
    ...(storage ? { storageUnit: unit } : {}),
  };
}

/** A fresh form whose size fields start in the account's units: 1 GB and 10 GB, or 1 GiB and 10 GiB. */
export function instanceCreateFormDefaults(units: ByteUnitSystem = DEFAULT_BYTE_UNITS): InstanceCreateFormState {
  const unit = defaultFormUnit(units);
  return { ...DEFAULT_INSTANCE_CREATE_FORM, memoryUnit: unit, storageUnit: unit, tags: [] };
}

export function isPositiveInteger(value: string): boolean {
  return /^[1-9]\d*$/.test(value.trim());
}
