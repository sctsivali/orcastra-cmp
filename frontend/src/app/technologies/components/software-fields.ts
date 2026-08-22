import type { NodeEnvironment, NodeStatus } from "@/types/api";

/**
 * Pure readers for the LXD environment block.
 *
 * No JSX, because every one of these encodes a fact about the payload that is easy to get
 * wrong and worth testing on its own.
 */

/**
 * A runtime the node reports, paired with its version.
 *
 * important: `driver` and `driver_version` are parallel pipe-separated lists, "lxc | qemu" and
 * "6.0.6 | 8.2.2". The helper this replaces split on the pipe and kept the LAST segment, so
 * LXC's version was dropped, and on a node reporting `driver: "lxc"` alone it displayed LXC's
 * version under a card hardcoded as QEMU/KVM. Zip by index and name each runtime.
 */
export interface DetectedRuntime {
  name: string;
  version?: string;
}

function splitPiped(value: unknown): string[] {
  return String(value ?? "")
    .split("|")
    .map((part) => part.trim())
    .filter(Boolean);
}

export function detectedRuntimes(env?: NodeEnvironment | null): DetectedRuntime[] {
  const names = splitPiped(env?.driver);
  const versions = splitPiped(env?.driver_version);
  if (names.length === 0) return [];
  // note: when the two lists disagree in length the pairing is unknowable, so names are
  // reported without versions rather than mis-paired. Guessing here would attribute one
  // runtime's version to another, which is the defect this replaces.
  if (names.length !== versions.length) return names.map((name) => ({ name }));
  return names.map((name, i) => ({ name, version: versions[i] }));
}

/** Storage backends the node has compiled in, paired with their versions. Same pipe shape. */
export function detectedStorage(env?: NodeEnvironment | null): DetectedRuntime[] {
  const names = splitPiped(env?.storage);
  const versions = splitPiped(env?.storage_version);
  if (names.length === 0) return [];
  if (names.length !== versions.length) return names.map((name) => ({ name }));
  return names.map((name, i) => ({ name, version: versions[i] }));
}

/**
 * important: LXD reports feature flags as the STRINGS "true" and "false", not as booleans.
 * `Boolean("false")` is `true`, so every flag would read as enabled.
 */
export function isFlagEnabled(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  return String(value ?? "").toLowerCase() === "true";
}

export function countEnabled(flags?: Record<string, string> | null): { on: number; total: number } {
  const entries = Object.entries(flags ?? {});
  return { on: entries.filter(([, v]) => isFlagEnabled(v)).length, total: entries.length };
}

/** Feature flags rendered as a key/value map the shared panel can take. */
export function flagsAsEntries(flags?: Record<string, string> | null): Record<string, string> {
  return Object.fromEntries(
    Object.entries(flags ?? {}).map(([key, value]) => [key, isFlagEnabled(value) ? "yes" : "no"]),
  );
}

/** The supported storage drivers list, flattened for display. */
export function supportedDriverLabels(env?: NodeEnvironment | null): string[] {
  const drivers = env?.storage_supported_drivers;
  if (!Array.isArray(drivers)) return [];
  return drivers
    .map((driver) => {
      const name = driver?.Name;
      if (!name) return "";
      return driver.Version ? `${name} ${driver.Version}` : name;
    })
    .filter(Boolean);
}

/**
 * What the fleet disagrees about.
 *
 * The page used to read connectedNodes[0] and label the result as the platform, so a fleet of
 * Ubuntu 24.04 on x86_64 and Ubuntu 22.04 on aarch64 was reported as 24.04 throughout. Naming
 * the divergence is the point of the per-node view, so it is stated rather than merely implied
 * by two cards the reader has to compare themselves.
 */
export function fleetDivergence(nodes: NodeStatus[]): string[] {
  const distinct = (pick: (env: NodeEnvironment) => unknown) =>
    new Set(
      nodes
        .map((node) => (node.environment ? pick(node.environment) : undefined))
        .filter((value): value is string => typeof value === "string" && value.length > 0),
    );

  const notes: string[] = [];
  const os = distinct((env) => `${env.os_name ?? ""} ${env.os_version ?? ""}`.trim());
  const kernels = distinct((env) => env.kernel_version);
  const arches = distinct((env) => env.kernel_architecture);
  const servers = distinct((env) => env.server_version);

  if (os.size > 1) notes.push(`${os.size} operating system versions`);
  if (kernels.size > 1) notes.push(`${kernels.size} kernels`);
  if (arches.size > 1) notes.push(`${arches.size} CPU architectures`);
  if (servers.size > 1) notes.push(`${servers.size} hypervisor versions`);
  return notes;
}
