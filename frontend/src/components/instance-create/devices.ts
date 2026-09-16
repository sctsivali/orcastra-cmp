// Device rows the create-instance form edits. Kept apart from the form so the row components
// and the form share one definition.

/** A network interface (NIC) attached to a managed LXD network at create time. */
export type NicDevice = {
  id: string;
  network: string; // managed network name
  name: string; // guest interface name (eth0, eth1, ...)
  // Optional fixed IPv4. LXD writes it as the device's ipv4.address, which pins the
  // allocation instead of leaving it to DHCP. Empty means DHCP.
  ipv4Address?: string;
};

/** An additional disk device attached at create time. */
export type DiskDevice = {
  id: string;
  kind: "volume" | "host"; // existing custom volume, or host path mount (containers)
  pool: string; // for volume
  source: string; // volume name (volume) or host path (host)
  path: string; // mount point inside the instance
};
