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
  return parseCidr(value, 4) !== null;
}

export function isValidIpv6Cidr(value: string): boolean {
  return parseCidr(value, 6) !== null;
}

/** Parse numeric addresses without accepting an incomplete IPv6 address or a zone ID. */
export function parseIpAddress(value: string, version: 4 | 6): bigint | null {
  if (version === 4) {
    const octets = value.split(".");
    if (octets.length !== 4 || octets.some((part) =>
      !/^(0|[1-9]\d{0,2})$/.test(part) || Number(part) > 255)) return null;
    return octets.reduce((address, part) => (address << BigInt(8)) | BigInt(part), BigInt(0));
  }
  let address = value;
  if (address.includes(".")) {
    const boundary = address.lastIndexOf(":");
    const tail = parseIpAddress(address.slice(boundary + 1), 4);
    if (boundary < 0 || tail === null) return null;
    address = `${address.slice(0, boundary + 1)}${(tail >> BigInt(16)).toString(16)}:${(tail & BigInt(65535)).toString(16)}`;
  }
  const halves = address.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const count = left.length + right.length;
  if ((halves.length === 1 && count !== 8) || (halves.length === 2 && count >= 8)) return null;
  if ([...left, ...right].some((group) => !/^[0-9a-fA-F]{1,4}$/.test(group))) return null;
  const groups = [...left, ...Array(8 - count).fill("0"), ...right];
  return groups.reduce<bigint>((result, group) => (result << BigInt(16)) | BigInt(`0x${group}`), BigInt(0));
}

export function parseCidr(value: string, version: 4 | 6) {
  const parts = value.trim().split("/");
  if (parts.length !== 2 || !/^\d{1,3}$/.test(parts[1])) return null;
  const prefix = Number(parts[1]);
  const bits = version === 4 ? 32 : 128;
  const address = parseIpAddress(parts[0], version);
  if (address === null || prefix > bits) return null;
  const hostBits = BigInt(bits - prefix);
  const network = (address >> hostBits) << hostBits;
  return { address, prefix, network, last: network + (BigInt(1) << hostBits) - BigInt(1) };
}

export function dhcpRangesError(value: string, version: 4 | 6, cidr?: string): string {
  if (!value.trim()) return "";
  const parts = value.split(",");
  if (value.length > 4096 || parts.length > 64) return "Use at most 64 DHCP ranges.";
  const subnet = cidr ? parseCidr(cidr, version) : null;
  const ranges: Array<[bigint, bigint]> = [];
  for (const part of parts) {
    const endpoints = part.trim().split("-");
    const first = endpoints.length === 2 ? parseIpAddress(endpoints[0].trim(), version) : null;
    const last = endpoints.length === 2 ? parseIpAddress(endpoints[1].trim(), version) : null;
    if (first === null || last === null || first > last) return `Use valid IPv${version} ranges in FIRST-LAST format.`;
    if (subnet && (first < subnet.network || last > subnet.last)) return "Ranges must be inside the network subnet.";
    if (subnet && first <= subnet.address && subnet.address <= last) return "Ranges must exclude the network gateway address.";
    if (subnet && version === 4 && subnet.prefix < 31 && (first === subnet.network || last === subnet.last)) {
      return "Ranges must exclude network and broadcast addresses.";
    }
    ranges.push([first, last]);
  }
  ranges.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  if (ranges.some((current, index) => index > 0 && current[0] <= ranges[index - 1][1])) return "Ranges must not overlap.";
  return "";
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
