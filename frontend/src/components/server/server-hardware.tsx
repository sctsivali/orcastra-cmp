"use client";

import { useServerResources } from "@/hooks/use-lxd";
import { formatBytes } from "@/lib/utils";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// LXD reports CPU frequency in MHz.
function formatFrequency(mhz?: number | null): string {
  if (!mhz) return "—";
  return mhz >= 1000 ? `${(mhz / 1000).toFixed(2)} GHz` : `${mhz} MHz`;
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right break-all">{value ?? "—"}</span>
    </div>
  );
}

// Like Row, but hides itself entirely when the value is empty - keeps the
// System/Firmware section from showing a wall of "—" on minimal hardware.
function OptRow({ label, value }: { label: string; value: React.ReactNode }) {
  if (value === null || value === undefined || value === "") return null;
  return <Row label={label} value={value} />;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <div className="text-sm font-medium text-muted-foreground mb-2">{children}</div>;
}

function joinParts(...parts: (string | null | undefined)[]): string | null {
  const v = parts.filter(Boolean).join(" ");
  return v || null;
}

// hideSerials drops asset identifiers (serial numbers, system UUID) - used on the broadly
// visible Technologies page where those add no value and are sensitive.
export function ServerHardware({
  node,
  hideSerials = false,
}: {
  node: string;
  hideSerials?: boolean;
}) {
  const { data, isLoading, isError } = useServerResources(node);
  const hw = (data as any)?.hardware;

  if (isLoading) {
    return <div className="text-sm text-muted-foreground">Loading hardware details...</div>;
  }
  if (isError || !hw) {
    return <div className="text-sm text-muted-foreground">Hardware details unavailable.</div>;
  }

  const cpu = hw.cpu || {};
  const memory = hw.memory || {};
  const system = hw.system;
  const gpuCards: any[] = hw.gpu_cards || [];
  const networks: any[] = hw.networks || [];
  const storage: any[] = hw.storage_devices || [];

  const fw = system?.firmware;
  const mb = system?.motherboard;
  const chassis = system?.chassis;
  const hasSystem =
    system &&
    (system.vendor || system.product || system.serial || fw?.version || mb?.product);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* System / firmware - full width, shown only when the hypervisor reports it */}
      {hasSystem && (
        <div className="lg:col-span-2">
          <SectionTitle>System & Firmware</SectionTitle>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8">
            <div>
              <OptRow label="Vendor" value={system.vendor} />
              <OptRow label="Product" value={system.product} />
              <OptRow label="Family" value={system.family} />
              <OptRow label="Version" value={system.version} />
              <OptRow label="Type" value={system.type} />
              <OptRow label="SKU" value={system.sku} />
              {!hideSerials && <OptRow label="Serial" value={system.serial} />}
              {!hideSerials && <OptRow label="UUID" value={system.uuid} />}
            </div>
            <div>
              {fw && (
                <>
                  <OptRow label="Firmware vendor" value={fw.vendor} />
                  <OptRow label="Firmware version" value={fw.version} />
                  <OptRow label="Firmware date" value={fw.date} />
                </>
              )}
              {mb && (
                <>
                  <OptRow label="Motherboard" value={joinParts(mb.vendor, mb.product)} />
                  <OptRow label="Motherboard version" value={mb.version} />
                  {!hideSerials && <OptRow label="Motherboard serial" value={mb.serial} />}
                </>
              )}
              {chassis && (
                <>
                  <OptRow label="Chassis" value={joinParts(chassis.vendor, chassis.type)} />
                  {!hideSerials && <OptRow label="Chassis serial" value={chassis.serial} />}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* CPU */}
      <div>
        <SectionTitle>CPU</SectionTitle>
        <Row label="Architecture" value={cpu.architecture} />
        <Row label="Logical CPUs" value={cpu.total} />
        <Row label="Cores" value={cpu.cores} />
        <Row label="Threads" value={cpu.threads} />
        <Row label="Sockets" value={Array.isArray(cpu.sockets) ? cpu.sockets.length : "—"} />
        {Array.isArray(cpu.sockets) && cpu.sockets.length > 0 && (
          <div className="mt-2">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Socket</TableHead>
                  <TableHead>Vendor</TableHead>
                  <TableHead>Cores</TableHead>
                  <TableHead>Threads</TableHead>
                  <TableHead>Frequency</TableHead>
                  <TableHead>Turbo</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {cpu.sockets.map((s: any, i: number) => (
                  <TableRow key={i}>
                    <TableCell>{s.socket ?? i}</TableCell>
                    <TableCell>{s.vendor || "—"}</TableCell>
                    <TableCell>{s.cores}</TableCell>
                    <TableCell>{s.threads}</TableCell>
                    <TableCell>{formatFrequency(s.frequency)}</TableCell>
                    <TableCell>{formatFrequency(s.frequency_turbo)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* Memory */}
      <div>
        <SectionTitle>Memory</SectionTitle>
        <Row label="Total" value={memory.total ? formatBytes(memory.total) : "—"} />
        <Row label="Used" value={memory.used ? formatBytes(memory.used) : "—"} />
        <Row label="Free" value={memory.free ? formatBytes(memory.free) : "—"} />
      </div>

      {/* GPU - full width */}
      <div className="lg:col-span-2">
        <SectionTitle>GPU{gpuCards.length ? ` (${gpuCards.length})` : ""}</SectionTitle>
        {gpuCards.length === 0 ? (
          <div className="text-sm text-muted-foreground">No GPU detected.</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {gpuCards.map((g: any, i: number) => (
              <div key={i} className="border rounded-lg p-3 space-y-1">
                <Row label="Product" value={joinParts(g.product) || g.product_id || "—"} />
                <Row label="Vendor" value={joinParts(g.vendor) || g.vendor_id || "—"} />
                <OptRow label="IDs" value={joinParts(g.vendor_id, g.product_id)} />
                <Row label="Driver" value={g.driver_version ? `${g.driver} ${g.driver_version}` : g.driver} />
                <Row label="PCI address" value={g.pci_address || g.usb_address} />
                {g.nvidia?.model && <Row label="NVIDIA model" value={g.nvidia.model} />}
                {g.nvidia?.cuda_version && <Row label="CUDA" value={g.nvidia.cuda_version} />}
                {g.nvidia?.nvrm_version && <Row label="NVRM" value={g.nvidia.nvrm_version} />}
                {g.drm?.card_device && <Row label="DRM" value={g.drm.card_device} />}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Storage devices - full width */}
      <div className="lg:col-span-2">
        <SectionTitle>Storage{storage.length ? ` (${storage.length})` : ""}</SectionTitle>
        {storage.length === 0 ? (
          <div className="text-sm text-muted-foreground">No storage devices reported.</div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Device</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Size</TableHead>
                  {!hideSerials && <TableHead>Serial</TableHead>}
                  <TableHead>Firmware</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {storage.map((d: any, i: number) => (
                  <TableRow key={i}>
                    <TableCell className="font-mono text-xs">{d.id || d.device || "—"}</TableCell>
                    <TableCell>{d.model || "—"}</TableCell>
                    <TableCell>{d.type || "—"}</TableCell>
                    <TableCell>{d.size ? formatBytes(d.size) : "—"}</TableCell>
                    {!hideSerials && (
                      <TableCell className="font-mono text-xs">{d.serial || "—"}</TableCell>
                    )}
                    <TableCell>{d.firmware_version || "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* Networks - full width */}
      <div className="lg:col-span-2">
        <SectionTitle>Network Interfaces{networks.length ? ` (${networks.length})` : ""}</SectionTitle>
        {networks.length === 0 ? (
          <div className="text-sm text-muted-foreground">No network cards reported.</div>
        ) : (
          <div className="space-y-3">
            {networks.map((n: any, i: number) => (
              <div key={i} className="border rounded-lg p-3 space-y-1">
                <Row label="Card" value={joinParts(n.vendor, n.product) || "—"} />
                <Row label="Driver" value={n.driver_version ? `${n.driver} (${n.driver_version})` : n.driver} />
                {n.firmware_version && <Row label="Firmware" value={n.firmware_version} />}
                <Row label="PCI address" value={n.pci_address} />
                {Array.isArray(n.ports) && n.ports.length > 0 && (
                  <div className="mt-2 overflow-x-auto">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Port</TableHead>
                          <TableHead>MAC</TableHead>
                          <TableHead>Link</TableHead>
                          <TableHead>Speed</TableHead>
                          <TableHead>Duplex</TableHead>
                          <TableHead>Auto-neg</TableHead>
                          <TableHead>Type</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {n.ports.map((p: any, j: number) => (
                          <TableRow key={j}>
                            <TableCell>{p.id ?? p.port ?? j}</TableCell>
                            <TableCell className="font-mono text-xs">{p.address || "—"}</TableCell>
                            <TableCell>{p.link_detected ? "Up" : "Down"}</TableCell>
                            <TableCell>{p.link_speed ? `${p.link_speed} Mbit/s` : "—"}</TableCell>
                            <TableCell>{p.link_duplex || "—"}</TableCell>
                            <TableCell>{p.auto_negotiation == null ? "—" : p.auto_negotiation ? "Yes" : "No"}</TableCell>
                            <TableCell>{p.port_type || p.transceiver_type || "—"}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
