import { Badge } from "@/components/ui/badge";
import { TableEmptyValue } from "@/components/ui/table-text";
import { useByteFormat } from "@/hooks/use-byte-format";
import {
  GPU_NOT_ALLOCATABLE,
  gpuCardLabel,
  gpuCardQualifier,
  gpuClassOf,
  gpuIdentifier,
} from "@/lib/gpu";
import { cn } from "@/lib/utils";
import type { GPUCardInfo } from "@/types/api";

import { GpuClassBadge } from "./gpu-class-badge";
import { OverflowText } from "@/components/ui/overflow-text";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-muted-foreground text-xs">{label}</p>
      {children}
    </div>
  );
}

interface GpuCardDetailsProps {
  card: GPUCardInfo;
  /** Cluster member modal: identity and memory only, no vendor deep-dive. */
  dense?: boolean;
}

/**
 * One GPU card. The in-use highlight goes through --primary rather than a raw palette step so
 * a personalized accent follows it, and the "attached to" line stays muted: --primary is
 * user-configurable and not contrast-clamped, so it must not be the only carrier of meaning.
 */
export function GpuCardDetails({ card, dense = false }: GpuCardDetailsProps) {
  const { formatBytes } = useByteFormat();
  const cls = gpuClassOf(card);
  const identifier = gpuIdentifier(card);

  return (
    <div
      className={cn(
        "rounded-lg border p-4 space-y-3",
        card.in_use ? "border-primary/50 bg-primary/5" : "border-border"
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="font-semibold">{gpuCardLabel(card)}</p>
            <GpuClassBadge card={card} />
            {cls === "display" ? (
              <Badge variant="muted">{GPU_NOT_ALLOCATABLE}</Badge>
            ) : card.in_use ? (
              <Badge variant="default">In use</Badge>
            ) : (
              <Badge variant="secondary">Available</Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            <OverflowText value={gpuCardQualifier(card)} label={`details for ${gpuCardLabel(card)}`} />
          </p>
          {card.used_by && (
            <p className="text-xs text-muted-foreground mt-1">Attached to {card.used_by}</p>
          )}
        </div>
        {card.nvidia?.brand && (
          <Badge variant="outline" className="text-xs shrink-0">
            {card.nvidia.brand}
          </Badge>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
        <Field label="Memory">
          {card.memory_total ? (
            <>
              <p className="font-medium">
                {formatBytes(card.memory_total)}
                {card.memory_shared ? " unified" : ""}
              </p>
              {card.memory_shared && (
                <p className="text-xs text-muted-foreground">Shared with system memory</p>
              )}
            </>
          ) : (
            <TableEmptyValue label="Memory not reported" />
          )}
        </Field>
        <Field label="Identifier">
          <p className="font-mono text-xs">{identifier.display}</p>
        </Field>
        <Field label="Driver">
          {card.driver ? (
            <>
              <p className="font-medium">{card.driver}</p>
              {card.driver_version && (
                <p className="text-xs text-muted-foreground">v{card.driver_version}</p>
              )}
            </>
          ) : (
            <TableEmptyValue label="No driver reported" />
          )}
        </Field>
        {card.vendor_id && card.product_id && (
          <Field label="Device ID">
            <p className="font-mono text-xs">
              {card.vendor_id}:{card.product_id}
            </p>
          </Field>
        )}
        {card.numa_node !== undefined && card.numa_node !== null && (
          <Field label="NUMA Node">
            <p className="font-medium">{card.numa_node}</p>
          </Field>
        )}
      </div>

      {!dense && card.nvidia && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm pt-2 border-t border-dashed">
          {card.nvidia.cuda_version && (
            <Field label="CUDA">
              <p className="font-medium">{card.nvidia.cuda_version}</p>
            </Field>
          )}
          {card.nvidia.architecture && (
            <Field label="Architecture">
              <p className="font-medium">{card.nvidia.architecture}</p>
            </Field>
          )}
          {card.nvidia.nvrm_version && (
            <Field label="NVRM Version">
              <p className="font-mono text-xs">{card.nvidia.nvrm_version}</p>
            </Field>
          )}
        </div>
      )}

      {!dense && card.drm?.card_name && (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm pt-2 border-t border-dashed">
          <Field label="DRM Card">
            <p className="font-mono text-xs">{card.drm.card_name}</p>
          </Field>
          {card.drm.render_name && (
            <Field label="Render Device">
              <p className="font-mono text-xs">{card.drm.render_name}</p>
            </Field>
          )}
        </div>
      )}

      {card.sriov && card.sriov.maximum_vfs > 0 && (
        <div className="grid grid-cols-3 gap-3 text-sm pt-2 border-t border-dashed">
          <Field label="SR-IOV VFs">
            <p className="font-medium">
              {card.sriov.current_vfs} / {card.sriov.maximum_vfs}
            </p>
          </Field>
          {card.sriov.vfs_count > 0 && (
            <Field label="Active VFs">
              <p className="font-medium">{card.sriov.vfs_count}</p>
            </Field>
          )}
        </div>
      )}

      {card.mdev_profiles && card.mdev_profiles.length > 0 && (
        <div className="pt-2 border-t border-dashed">
          <p className="text-muted-foreground text-xs mb-2">vGPU Profiles</p>
          <div className="flex flex-wrap gap-2">
            {card.mdev_profiles.map((profile, index) => (
              <Badge key={`${profile.name}-${index}`} variant="outline" className="text-xs">
                {profile.name} ({profile.available} available)
              </Badge>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
