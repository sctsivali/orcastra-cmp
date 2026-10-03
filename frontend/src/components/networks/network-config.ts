import { dhcpRangesError, ipv4AddressError, ipv6AddressError, type IpMode, type NetworkType } from "./network-rules";

export type BooleanOverride = "default" | "true" | "false";
export type DnsMode = "default" | "managed" | "dynamic" | "none";
export interface AddressOptions {
  mode: IpMode;
  custom: string;
  nat: BooleanOverride;
  dhcp: BooleanOverride;
  ranges: string;
}
export interface NetworkOptions {
  ipv4: AddressOptions;
  ipv6: AddressOptions;
  stateful: BooleanOverride;
  mtu: string;
  dnsDomain: string;
  dnsMode: DnsMode;
  acls: string[];
}

export function defaultNetworkOptions(): NetworkOptions {
  const address = (): AddressOptions => ({ mode: "auto", custom: "", nat: "default", dhcp: "default", ranges: "" });
  return { ipv4: address(), ipv6: address(), stateful: "default", mtu: "", dnsDomain: "", dnsMode: "default", acls: [] };
}

export function buildNetworkConfig(type: NetworkType, uplink: string, options: NetworkOptions, supportedKeys?: string[]) {
  const config: Record<string, string> = {};
  const set = (key: string, value: string) => {
    if (value && (!supportedKeys || supportedKeys.includes(key))) config[key] = value;
  };
  for (const version of [4, 6] as const) {
    const address = options[`ipv${version}`];
    if (address.mode !== "auto") set(`ipv${version}.address`, address.mode === "custom" ? address.custom.trim() : "none");
    if (address.mode === "none") continue;
    if (address.nat !== "default") set(`ipv${version}.nat`, address.nat);
    if (address.dhcp !== "default") set(`ipv${version}.dhcp`, address.dhcp);
    if (address.dhcp !== "false") {
      if (address.mode === "custom" && (version === 4 || (type === "bridge" && options.stateful === "true"))) set(`ipv${version}.dhcp.ranges`, address.ranges.trim());
      if (version === 6 && options.stateful !== "default") set("ipv6.dhcp.stateful", options.stateful);
    }
  }
  if (type === "bridge") {
    set("bridge.mtu", options.mtu.trim());
    if (options.dnsMode !== "default") set("dns.mode", options.dnsMode);
  } else {
    set("network", uplink);
  }
  set("dns.domain", options.dnsDomain.trim());
  if (options.acls.length) set("security.acls", options.acls.join(","));
  return config;
}

export function networkOptionsErrors(options: NetworkOptions, type: NetworkType, supportedKeys?: string[]) {
  const supported = (key: string) => !supportedKeys || supportedKeys.includes(key);
  const errors: Record<string, string> = {
    ipv4: ipv4AddressError(options.ipv4.mode, options.ipv4.custom),
    ipv6: ipv6AddressError(options.ipv6.mode, options.ipv6.custom),
  };
  for (const version of [4, 6] as const) {
    const address = options[`ipv${version}`];
    const key = `ipv${version}.dhcp.ranges`;
    if (address.mode === "custom" && address.dhcp !== "false" && supported(key) &&
        (version === 4 || (type === "bridge" && options.stateful === "true"))) {
      errors[key] = dhcpRangesError(address.ranges, version, address.mode === "custom" ? address.custom : undefined);
    }
  }
  if (type === "bridge" && options.mtu.trim()) {
    const mtu = Number(options.mtu);
    if (!/^\d+$/.test(options.mtu.trim()) || mtu < 68 || mtu > 65535) errors.mtu = "Use an integer MTU between 68 and 65535.";
    else if (options.ipv6.mode !== "none" && mtu < 1280) errors.mtu = "MTU must be at least 1280 when IPv6 is enabled.";
  }
  const domain = options.dnsDomain.trim().replace(/\.$/, "");
  if (options.dnsDomain.trim() && (!domain || domain.length > 253 || domain.split(".").some((label) => !/^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/.test(label)))) {
    errors.dnsDomain = "Enter a valid DNS domain.";
  }
  return errors;
}
