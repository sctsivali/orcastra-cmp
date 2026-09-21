import { APIError, type InstanceCreateIssue } from "@/types/api";

export type CreateErrorField =
  | "name"
  | "image"
  | "profiles"
  | "storage"
  | "network"
  | "devices"
  | "cpu"
  | "memory"
  | "instances";

export interface CreateError {
  /** What the banner shows: the API's sentence, or a generic one. */
  message: string;
  /** Set when the refusal names a field the operator can change. */
  field?: CreateErrorField;
  limit?: string;
  project?: string;
  max?: string;
}

const FIELD_BY_LIMIT: Record<string, CreateErrorField> = {
  "limits.cpu": "cpu",
  "limits.memory": "memory",
  "limits.instances": "instances",
  "limits.containers": "instances",
  "limits.virtual-machines": "instances",
};

function mapBlockerField(
  field: string | null | undefined,
  code: string,
): CreateErrorField | undefined {
  const normalizedCode = code.toUpperCase();
  if (normalizedCode.includes("INSTANCE_NAME") || normalizedCode.includes("DUPLICATE_NAME")) {
    return "name";
  }
  if (normalizedCode.includes("IMAGE") || normalizedCode.includes("SOURCE")) return "image";
  if (normalizedCode.includes("PROFILE")) return "profiles";
  if (normalizedCode.includes("NETWORK") || normalizedCode.includes("NIC")) return "network";
  if (
    normalizedCode.includes("STORAGE") ||
    normalizedCode.includes("ROOT") ||
    normalizedCode.includes("DISK") ||
    normalizedCode.includes("POOL")
  ) return "storage";
  if (!field) return undefined;
  const normalized = field.toLowerCase();
  if (normalized === "name" || normalized === "instance_name") return "name";
  if (normalized.includes("image") || normalized.includes("source")) return "image";
  if (normalized.includes("profile")) return "profiles";
  if (normalized === "cpu_count" || normalized === "cpu" || normalized === "limits.cpu") {
    return "cpu";
  }
  if (
    normalized === "memory_bytes" ||
    normalized === "memory" ||
    normalized === "limits.memory"
  ) return "memory";
  if (normalized === "instances") return "instances";
  if (FIELD_BY_LIMIT[normalized]) return FIELD_BY_LIMIT[normalized];
  if (normalized.includes("network") || normalized.includes("nic")) return "network";
  if (
    normalized.includes("root") ||
    normalized.includes("storage") ||
    normalized.includes("pool") ||
    normalized.includes("disk")
  ) return "storage";
  if (normalized.includes("device")) return "devices";
  return undefined;
}

const AGGREGATE = /Reached maximum aggregate value "([^"]+)" for "(limits\.[a-z-]+)" in project "([^"]+)"/;
const COUNT = /Reached maximum number of instances(?: of type "([^"]+)")? in project "([^"]+)"/;

/**
 * Reads a failed create into something the form can act on. The API answers a project-limit
 * refusal as a structured 400 (code PROJECT_LIMIT_REACHED); an older API or a proxy that
 * flattened it still carries LXD's sentence, which the regexes read the same way.
 */
export function mapCreateError(error: unknown): CreateError {
  const message =
    error instanceof Error && error.message ? error.message : "Failed to create instance";
  if (error instanceof APIError && error.detail && typeof error.detail === "object") {
    const detail = error.detail as Record<string, unknown>;
    if (detail.code === "PROJECT_LIMIT_REACHED" && typeof detail.limit === "string") {
      return {
        message: typeof detail.message === "string" ? detail.message : message,
        field: FIELD_BY_LIMIT[detail.limit],
        limit: detail.limit,
        project: typeof detail.project === "string" ? detail.project : undefined,
        max: typeof detail.max === "string" ? detail.max : undefined,
      };
    }
  }
  const aggregate = AGGREGATE.exec(message);
  if (aggregate) {
    return { message, field: FIELD_BY_LIMIT[aggregate[2]], limit: aggregate[2], project: aggregate[3], max: aggregate[1] };
  }
  const count = COUNT.exec(message);
  if (count) {
    const limit = count[1] === "virtual-machine" ? "limits.virtual-machines" : count[1] === "container" ? "limits.containers" : "limits.instances";
    return { message, field: "instances", limit, project: count[2] };
  }
  return { message };
}

/** Turn structured preflight blockers into the same inline errors as a daemon refusal. */
export function mapCreatePreflightBlockers(
  blockers: InstanceCreateIssue[],
): CreateError {
  const message = blockers.map((blocker) => blocker.message).join(" ") ||
    "The create request did not pass preflight checks.";
  const field = blockers
    .map((blocker) => mapBlockerField(blocker.field, blocker.code))
    .find((candidate) => candidate !== undefined);
  return { message, field };
}

/** The sentence under the field the cluster refused. */
export function fieldRefusalText(err: CreateError): string {
  if (!err.limit || !["cpu", "memory", "instances"].includes(err.field ?? "")) {
    return err.message;
  }
  const project = err.project ? `${err.project} allows` : "The project allows";
  if (err.field === "cpu") return `The cluster refused this value: ${project} ${err.max ?? "this many"} vCPU in total across its instances.`;
  if (err.field === "memory") return `The cluster refused this value: ${project} ${err.max ?? "this much"} memory in total across its instances.`;
  return `The cluster refused this create: ${project} no more instances of this kind.`;
}
