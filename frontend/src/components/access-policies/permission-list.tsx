import type { TenantPermission } from "@/types";
import { TENANT_PERMISSION_LABELS } from "@/types";

/**
 * A rule's permissions as plain text, for a table cell.
 *
 * note: whole labels. The chips this replaces kept only the first word, so "File Transfer" read
 * "File" and "Console & Terminal" read "Console".
 */
export function PermissionList({ permissions }: { permissions: string[] }) {
  const labels = (permissions as TenantPermission[])
    .filter((p) => p !== "snapshot")
    .map((p) => TENANT_PERMISSION_LABELS[p] ?? p);
  return <>{labels.join(", ")}</>;
}
