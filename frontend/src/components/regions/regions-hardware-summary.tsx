"use client";

import * as React from "react";
import { Cpu, HardDrive, MemoryStick, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatBytes } from "@/lib/utils";
import { GPU_EMPTY_TITLE, GPU_NO_COMPUTE_TITLE } from "@/lib/gpu";
import { ExtendedHardwareInfo } from "@/types/api";

/**
 * Aggregate CPU, memory, storage and GPU for one region, in a dense chip row (compact) or
 * a labelled grid. Shared by the region card's collapsed header and its expanded body.
 */
export function HardwareSummary({ hardware, compact = false }: { hardware: ExtendedHardwareInfo; compact?: boolean }) {
  // Compute GPUs and detected cards are different numbers about the same hardware, so the
  // card count only appears when it says something the GPU count did not.
  const cardsTotal = hardware.gpu_cards_total ?? hardware.gpu_total;
  const extraCards = Math.max(0, cardsTotal - hardware.gpu_total);
  if (compact) {
    return (
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge variant="outline" className="gap-1">
          <Cpu className="h-3 w-3 text-foreground" />
          {hardware.cpu_total} hardware threads
        </Badge>
        <Badge variant="outline" className="gap-1">
          <MemoryStick className="h-3 w-3 text-foreground" />
          {formatBytes(hardware.memory_total)}
        </Badge>
        {hardware.gpu_total > 0 && (
          <Badge variant="outline" className="gap-1">
            <Sparkles className="h-3 w-3 text-foreground" />
            {hardware.gpu_total} GPU
          </Badge>
        )}
        <Badge variant="outline" className="gap-1">
          <HardDrive className="h-3 w-3 text-foreground" />
          {formatBytes(hardware.storage_total)}
        </Badge>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-4">
      {/* CPU */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Cpu className="h-4 w-4 text-foreground" />
          CPU
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          <p>{hardware.cpu_total} hardware threads ({hardware.cpu_sockets} sockets)</p>
          <p>{hardware.cpu_architecture}</p>
          {hardware.cpu_vendor && <p>{hardware.cpu_vendor}</p>}
        </div>
      </div>

      {/* Memory */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <MemoryStick className="h-4 w-4 text-foreground" />
          Memory
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          <p>{formatBytes(hardware.memory_total)}</p>
          <p>{formatBytes(hardware.memory_used)} used</p>
        </div>
      </div>

      {/* GPU */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4 text-foreground" />
          GPU
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          {hardware.gpu_total > 0 ? (
            <>
              <p>
                {hardware.gpu_total} compute {hardware.gpu_total === 1 ? "GPU" : "GPUs"}
              </p>
              {hardware.gpu_vendors.length > 0 && (
                <p>{hardware.gpu_vendors.join(", ")}</p>
              )}
              {hardware.gpu_models.length > 0 && (
                <p className="break-words">{hardware.gpu_models.join(", ")}</p>
              )}
              {extraCards > 0 && <p className="text-xs">{cardsTotal} cards detected</p>}
            </>
          ) : cardsTotal > 0 ? (
            <>
              <p>{GPU_NO_COMPUTE_TITLE}</p>
              <p className="text-xs">{cardsTotal} cards detected</p>
            </>
          ) : (
            <p>{GPU_EMPTY_TITLE}</p>
          )}
        </div>
      </div>

      {/* Storage */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <HardDrive className="h-4 w-4 text-foreground" />
          Storage
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          <p>{formatBytes(hardware.storage_total)}</p>
          {hardware.storage_types.length > 0 && (
            <p>{hardware.storage_types.join(", ")}</p>
          )}
          {hardware.storage_drivers.length > 0 && (
            <p className="text-xs">{hardware.storage_drivers.join(", ")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

// Region card component
