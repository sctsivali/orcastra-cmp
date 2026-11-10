import type { TenantPermission } from "@/types";
import { TENANT_PERMISSION_LABELS } from "@/types";

/**
 * A rule's permissions as plain text, for a table cell.
 *
 * Whole labels: a multi-word label such as "File Transfer" must not shrink to its first word.
 */
export function PermissionList({ permissions }: { permissions: string[] }) {
  const labels = (permissions as TenantPermission[])
    .filter((p) => p !== "snapshot")
    .map((p) => TENANT_PERMISSION_LABELS[p] ?? p);
  return <>{labels.join(", ")}</>;
}
