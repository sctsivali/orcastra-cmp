import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { formatBytes, formatMemory } from "@/lib/utils";
import type {
  InstanceOperationJob,
  InstanceSpecificationChange,
} from "@/types/api";

function fieldLabel(field: string): string {
  if (field === "cpu_count") return "CPU count";
  if (field === "memory_bytes") return "Memory limit";
  if (field === "root_disk_bytes") return "Root disk size";
  return field;
}

function changeValue(change: InstanceSpecificationChange, value: number | null): string {
  if (value === null) return "Not set";
  if (change.field === "cpu_count") return String(value);
  return change.field === "root_disk_bytes" ? formatBytes(value) : formatMemory(value);
}

export function ChangeSummary({ changes }: { changes: InstanceSpecificationChange[] }) {
  return (
    <dl className="divide-y rounded-md border">
      {changes.map((change) => (
        <div
          key={change.field}
          className="grid gap-1 px-3 py-2 text-sm sm:grid-cols-[10rem_minmax(0,1fr)]"
        >
          <dt className="font-medium">{fieldLabel(change.field)}</dt>
          <dd className="text-muted-foreground">
            {changeValue(change, change.before)} → {changeValue(change, change.after)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function numberAt(parent: Record<string, unknown> | null, child: string, field: string): number | null {
  const nested = record(parent?.[child]);
  return typeof nested?.[field] === "number" ? nested[field] as number : null;
}

function readbackFor(job: InstanceOperationJob): Record<string, unknown> | null {
  if (job.verified_readback) return job.verified_readback;
  return record(record(job.result)?.specification);
}

export function OperationReadback({ job }: { job: InstanceOperationJob }) {
  const readback = readbackFor(job);
  const result = record(job.result);
  const cpu = numberAt(readback, "cpu", "value");
  const memory = numberAt(readback, "memory", "value_bytes");
  const disk = numberAt(readback, "root_disk", "value_bytes");
  const status = typeof readback?.status === "string"
    ? readback.status
    : typeof result?.instance_status === "string" ? result.instance_status : null;
  const hasValues = cpu !== null || memory !== null || disk !== null || status !== null;
  const guestActionRequired = result?.guest_action_required === true;

  return (
    <div className="space-y-3">
      {hasValues && (
        <div>
          <h3 className="text-sm font-semibold">
            {job.verified_readback ? "Verified cluster readback" : "Current cluster readback"}
          </h3>
          <dl className="mt-2 grid gap-x-4 gap-y-2 rounded-md border p-3 text-sm sm:grid-cols-2">
            {cpu !== null && <div><dt className="text-muted-foreground">CPU count</dt><dd>{cpu}</dd></div>}
            {memory !== null && <div><dt className="text-muted-foreground">Memory limit</dt><dd>{formatMemory(memory)}</dd></div>}
            {disk !== null && <div><dt className="text-muted-foreground">Root disk</dt><dd>{formatBytes(disk)}</dd></div>}
            {status !== null && <div><dt className="text-muted-foreground">Instance status</dt><dd>{status}</dd></div>}
          </dl>
        </div>
      )}
      {guestActionRequired && (
        <Alert>
          <AlertTitle>Guest verification required</AlertTitle>
          <AlertDescription>
            The cluster applied the VM limits live. Confirm CPU and memory inside the guest. Guest
            support may require an in-guest action or a later reboot before the new capacity is visible.
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
