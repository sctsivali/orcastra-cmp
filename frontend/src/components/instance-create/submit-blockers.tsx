export interface SubmitBlockerInput {
  nameValid: boolean;
  hasNode: boolean;
  bootFromIso: boolean;
  hasImage: boolean;
  /** A local image is picked but the store that has to hold it has not answered yet. */
  imageStoreLoading: boolean;
  hasIso: boolean;
  resourcesValid: boolean;
  hasPool: boolean;
  disksValid: boolean;
  nicsValid: boolean;
  gpusValid: boolean;
  cpuWithinLimit: boolean;
  memoryWithinLimit: boolean;
}

export interface SubmitBlocker {
  id: string;
  reason: string;
}

/**
 * What still stands between the form and a create, in the order the form asks for them. The
 * Create buttons are disabled exactly when this is non-empty, so the line under them and the
 * disabled state can never disagree.
 */
export function submitBlockers(input: SubmitBlockerInput): SubmitBlocker[] {
  const out: SubmitBlocker[] = [];
  if (!input.nameValid) out.push({ id: "name", reason: "a valid name" });
  if (!input.hasNode) out.push({ id: "node", reason: "a node" });
  if (input.bootFromIso ? !input.hasIso : !input.hasImage) {
    out.push({ id: "source", reason: input.bootFromIso ? "a boot ISO" : "a base image" });
  } else if (!input.bootFromIso && input.imageStoreLoading) {
    // A seeded or carried-over local pick resolves only inside the target project's store,
    // so the create waits for that store to answer rather than sending a fingerprint it may
    // not hold.
    out.push({ id: "image-store", reason: "the project's image store to finish loading" });
  }
  if (!input.resourcesValid) {
    out.push({ id: "resources", reason: "CPU, memory and storage as positive whole numbers" });
  }
  if (!input.cpuWithinLimit) out.push({ id: "cpu-limit", reason: "CPU within the project limit" });
  if (!input.memoryWithinLimit) out.push({ id: "memory-limit", reason: "memory within the project limit" });
  if (input.hasNode && !input.hasPool) out.push({ id: "pool", reason: "a storage pool" });
  if (!input.disksValid) out.push({ id: "disks", reason: "every additional disk completed" });
  if (!input.nicsValid) {
    out.push({ id: "nics", reason: "every network interface with a network and a unique name" });
  }
  if (!input.gpusValid) out.push({ id: "gpus", reason: "every GPU with a device" });
  return out;
}

export function SubmitBlockers({ id, blockers }: { id: string; blockers: SubmitBlocker[] }) {
  if (blockers.length === 0) return null;
  return (
    <p id={id} role="status" className="text-xs text-muted-foreground">
      Not ready to create: {blockers.map((b) => b.reason).join(", ")}.
    </p>
  );
}
