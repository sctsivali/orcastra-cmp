import {
  Box,
  Cable,
  Cpu,
  HardDrive,
  Network,
  Sparkles,
  Usb,
  type LucideIcon,
} from "lucide-react";

// The LXD device-type glyphs, extracted from ui/clickable-badge.tsx so the profiles dialog and
// the instance devices tab share one map instead of drifting apart.
const ICONS: Record<string, LucideIcon> = {
  disk: HardDrive,
  nic: Network,
  gpu: Sparkles,
  usb: Usb,
  unix: Cable,
  proxy: Cable,
  infiniband: Network,
  tpm: Box,
  pci: Cpu,
  none: Box,
};

export function DeviceIcon({ type, className }: { type?: string; className?: string }) {
  const Icon = ICONS[(type ?? "").toLowerCase()] ?? Box;
  return (
    <span
      className={
        className ??
        "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-foreground text-background"
      }
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
    </span>
  );
}
