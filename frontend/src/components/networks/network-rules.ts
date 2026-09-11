// Pure rules for the Create Network dialog: what LXD will and will not accept, checked before
// the request is built so the operator reads the reason next to the field, not in a toast.

export type IpMode = "auto" | "none" | "custom";
export type NetworkType = "bridge" | "ovn";

// Orcastra only supports Bridge and OVN networks.
export const NETWORK_TYPES: ReadonlyArray<{ value: NetworkType; label: string }> = [
  { value: "bridge", label: "Bridge" },
  { value: "ovn", label: "OVN" },
];

/** LXD wants a real CIDR: "not-an-ip", a bare address, or /33 must be rejected. */
export function isValidIpv4Cidr(value: string): boolean {
  const m = value.trim().match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/);
  if (!m) return false;
  const octets = [m[1], m[2], m[3], m[4]].map(Number);
  if (octets.some((o) => o > 255)) return false;
  const prefix = Number(m[5]);
  return prefix >= 0 && prefix <= 32;
}

export function isValidIpv6Cidr(value: string): boolean {
  const m = value.trim().match(/^([0-9a-fA-F:]+)\/(\d{1,3})$/);
  if (!m) return false;
  const prefix = Number(m[2]);
  if (prefix > 128) return false;
  const addr = m[1];
  if ((addr.match(/::/g) || []).length > 1) return false; // at most one "::"
  const groups = addr.split(":").filter((g) => g !== "");
  if (groups.length === 0 || groups.length > 8) return false;
  return groups.every((g) => /^[0-9a-fA-F]{1,4}$/.test(g));
}

// Bridge network names become Linux interface names (<= 15 chars). OVN names don't map to a
// host interface, so they can be longer. LXD also rejects a leading hyphen and all-digit
// names, so require a leading letter.
export function networkNameMax(type: NetworkType): number {
  return type === "bridge" ? 15 : 63;
}

export function networkNameError(name: string, type: NetworkType): string {
  if (name === "") return "";
  if (!/^[a-zA-Z][a-zA-Z0-9-]*$/.test(name)) {
    return "Must start with a letter and contain only letters, numbers and hyphens.";
  }
  const max = networkNameMax(type);
  if (name.length > max) return `Name must be at most ${max} characters (${type}).`;
  return "";
}

export function ipv4AddressError(mode: IpMode, custom: string): string {
  if (mode !== "custom") return "";
  if (custom.trim() === "") return "Enter an IPv4 address in CIDR notation.";
  if (!isValidIpv4Cidr(custom)) return "Invalid IPv4 CIDR, for example 10.10.10.1/24.";
  return "";
}

export function ipv6AddressError(mode: IpMode, custom: string): string {
  if (mode !== "custom") return "";
  if (custom.trim() === "") return "Enter an IPv6 address in CIDR notation.";
  if (!isValidIpv6Cidr(custom)) return "Invalid IPv6 CIDR, for example fd42:1:2:3::1/64.";
  return "";
}
