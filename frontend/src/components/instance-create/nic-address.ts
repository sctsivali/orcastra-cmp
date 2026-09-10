/**
 * Validating a fixed IPv4 for an instance NIC, in the browser.
 *
 * Enterprise workloads (a database cluster, DNS, a load balancer, an API gateway) need their
 * address decided at provisioning time rather than discovered afterwards, and LXD's own web UI
 * has no control for it. The address is written as the NIC device's `ipv4.address`.
 *
 * The backend validates this too, and it is the authority. This exists so the operator finds
 * out while the field is in front of them, not after a submit.
 */

export type AddressCheck = { ok: true } | { ok: false; reason: string };

const OCTETS = 4;

function parseIPv4(value: string): number[] | null {
  const parts = value.trim().split(".");
  if (parts.length !== OCTETS) return null;
  const octets: number[] = [];
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const n = Number(part);
    if (n > 255) return null;
    octets.push(n);
  }
  return octets;
}

function toInt(octets: number[]): number {
  return octets.reduce((acc, octet) => acc * 256 + octet, 0);
}

/**
 * The CIDR an LXD managed network hands out from, taken from its `ipv4.address` config.
 * Returns null when the network has no IPv4 subnet, which "none" and "auto" both mean.
 */
export function subnetOf(config: Record<string, string> | undefined): string | null {
  const raw = config?.["ipv4.address"];
  if (!raw || raw === "none" || raw === "auto") return null;
  return raw;
}

export function checkFixedAddress(
  address: string,
  networkConfig: Record<string, string> | undefined,
): AddressCheck {
  const trimmed = address.trim();
  if (!trimmed) return { ok: true };

  const parsed = parseIPv4(trimmed);
  if (!parsed) return { ok: false, reason: "Not a valid IPv4 address." };

  const cidr = subnetOf(networkConfig);
  if (!cidr) {
    return {
      ok: false,
      reason: "This network has no IPv4 subnet to allocate a fixed address from.",
    };
  }

  const [base, prefixRaw] = cidr.split("/");
  const baseOctets = parseIPv4(base);
  const prefix = Number(prefixRaw);
  if (!baseOctets || !Number.isInteger(prefix) || prefix < 0 || prefix > 32) {
    return { ok: true }; // unparseable config is not the operator's mistake to answer for
  }

  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  const network = (toInt(baseOctets) & mask) >>> 0;
  const broadcast = (network | (~mask >>> 0)) >>> 0;
  const candidate = toInt(parsed) >>> 0;

  if (candidate < network || candidate > broadcast) {
    return { ok: false, reason: `Outside this network's subnet ${cidr}.` };
  }
  if (prefix <= 30 && (candidate === network || candidate === broadcast)) {
    return {
      ok: false,
      reason:
        candidate === network
          ? "That is the network address."
          : "That is the broadcast address.",
    };
  }
  return { ok: true };
}
