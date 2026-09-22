import type {
  EditableSpecificationSource,
  InstanceSpecificationDetail,
  InstanceSpecificationPreflightRequest,
} from "@/types/api";

export type SizeUnit = "MiB" | "GiB";

export interface SizeDraft {
  value: string;
  unit: SizeUnit;
}

const UNIT_BYTES: Record<SizeUnit, number> = {
  MiB: 1024 ** 2,
  GiB: 1024 ** 3,
};

export function draftFromBytes(
  bytes: number | null,
  preferred: SizeUnit = "GiB",
): SizeDraft {
  if (bytes === null) return { value: "", unit: preferred };
  const unit = bytes >= UNIT_BYTES.GiB ? "GiB" : "MiB";
  return { value: String(bytes / UNIT_BYTES[unit]), unit };
}

export function bytesFromDraft(draft: SizeDraft): number | null {
  if (!draft.value.trim()) return null;
  const numeric = Number(draft.value);
  if (!Number.isFinite(numeric) || numeric <= 0) return null;
  const bytes = Math.round(numeric * UNIT_BYTES[draft.unit]);
  return Number.isSafeInteger(bytes) && bytes > 0 ? bytes : null;
}

export function sourceLabel(source: EditableSpecificationSource): string {
  if (source === "instance") return "Instance override";
  if (source === "profile") return "Inherited from profile";
  return "Daemon default";
}

export interface SpecificationDraft {
  cpu: string;
  memory: SizeDraft;
  rootDisk: SizeDraft;
}

export function draftFromSpecification(
  specification: InstanceSpecificationDetail,
): SpecificationDraft {
  return {
    cpu:
      specification.cpu.value === null
        ? ""
        : String(specification.cpu.value),
    memory: draftFromBytes(specification.memory.value_bytes),
    rootDisk: draftFromBytes(specification.root_disk.value_bytes),
  };
}

export interface SpecificationValidation {
  request: InstanceSpecificationPreflightRequest | null;
  errors: {
    cpu?: string;
    memory?: string;
    rootDisk?: string;
    form?: string;
  };
  memoryDecreases: boolean;
  rootDiskGrows: boolean;
  overridesInherited: boolean;
}

export function validateSpecificationDraft(
  specification: InstanceSpecificationDetail,
  draft: SpecificationDraft,
): SpecificationValidation {
  const errors: SpecificationValidation["errors"] = {};
  const request: InstanceSpecificationPreflightRequest = {
    host: specification.host,
    project: specification.project,
  };

  const cpuText = draft.cpu.trim();
  if (cpuText) {
    const cpu = Number(cpuText);
    if (!Number.isSafeInteger(cpu) || cpu <= 0) {
      errors.cpu = "Enter a positive whole number.";
    } else if (cpu !== specification.cpu.value) {
      request.cpu_count = cpu;
    }
  } else if (specification.cpu.value !== null) {
    errors.cpu = "vCPU cannot be empty. Profile reset is not available here.";
  }

  const memoryText = draft.memory.value.trim();
  const memoryBytes = memoryText ? bytesFromDraft(draft.memory) : null;
  if (memoryText && memoryBytes === null) {
    errors.memory = "Enter a positive memory limit.";
  } else if (!memoryText && specification.memory.value_bytes !== null) {
    errors.memory = "Memory cannot be empty. Profile reset is not available here.";
  } else if (memoryBytes !== specification.memory.value_bytes) {
    if (memoryBytes !== null) request.memory_bytes = memoryBytes;
  }

  const rootText = draft.rootDisk.value.trim();
  const rootBytes = rootText ? bytesFromDraft(draft.rootDisk) : null;
  if (rootText && rootBytes === null) {
    errors.rootDisk = "Enter a positive disk size.";
  } else if (rootBytes !== null && rootBytes !== specification.root_disk.value_bytes) {
    if (!specification.root_disk.editable) {
      errors.rootDisk =
        specification.root_disk.reason || "This root disk cannot be resized.";
    } else if (
      specification.root_disk.value_bytes !== null &&
      rootBytes <= specification.root_disk.value_bytes
    ) {
      errors.rootDisk = "Root disk size can only be increased.";
    } else {
      request.root_disk_bytes = rootBytes;
    }
  }

  const hasChanges =
    request.cpu_count !== undefined ||
    request.memory_bytes !== undefined ||
    request.root_disk_bytes !== undefined;
  if (!hasChanges && Object.keys(errors).length === 0) {
    errors.form = "Change at least one specification before applying.";
  }

  const memoryDecreases =
    request.memory_bytes !== undefined &&
    specification.memory.value_bytes !== null &&
    request.memory_bytes < specification.memory.value_bytes;
  const rootDiskGrows = request.root_disk_bytes !== undefined;
  const overridesInherited =
    (request.cpu_count !== undefined && specification.cpu.source === "profile") ||
    (request.memory_bytes !== undefined && specification.memory.source === "profile") ||
    (request.root_disk_bytes !== undefined && specification.root_disk.source === "profile");

  return {
    request: Object.keys(errors).length === 0 ? request : null,
    errors,
    memoryDecreases,
    rootDiskGrows,
    overridesInherited,
  };
}
