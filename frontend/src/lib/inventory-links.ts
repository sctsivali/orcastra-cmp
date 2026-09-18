import { serialiseFacet } from "@/lib/filter-url";
import { WARNING_SEVERITY_BUCKETS, type SeverityBucket } from "@/lib/warning-facets";

// Every link into a filtered list is built here, and only here.
//
// important: a list page reads its filters from the URL: one key per column, namespaced by
// the table's id where a page holds several tables (`all.severity`, `nodes.cluster`), and the
// value compared with exact equality against what the column's facet accessor returns. A link
// written by hand at the source therefore breaks in three quiet ways: the key is spelled for
// the wrong table, the value is the display name where the accessor returns the id, or the
// encoding differs from the one the filter engine expects. Each of the three had shipped at
// least once ("No warning matches the current filters." on a cluster with five warnings).
// `scripts/checks/check-inventory-links.mjs` proves every key below against the target page's
// source and refuses a hand-built inventory href anywhere else.

/**
 * The targets and the query keys each one reads. `facets` are enum keys the target parses
 * with parseFacet; `reserved` keys are read raw by the page itself. One entry per line: the
 * guard parses this block.
 */
export const LINK_TARGETS = {
  instances: { path: "/inventory/instances", search: true, facets: ["host", "project"] },
  clusters: { path: "/inventory/clusters", search: true, facets: [] },
  certificates: { path: "/inventory/clusters/certificates", search: true, facets: ["cluster"] },
  members: { path: "/inventory/clustering/members", search: false, facets: [], reserved: ["node"] },
  warnings: { path: "/inventory/warnings", search: false, facets: ["all.severity", "all.node", "certificates.cluster", "nodes.cluster"], reserved: ["tab"] },
  images: { path: "/inventory/images", search: true, facets: ["node", "project"] },
  profiles: { path: "/inventory/profiles", search: true, facets: ["node", "project"] },
  operations: { path: "/inventory/operations", search: true, facets: ["node"] },
  networkAcls: { path: "/inventory/network-acls", search: true, facets: ["node"] },
  storagePools: { path: "/inventory/storage-pools", search: true, facets: ["node"] },
  storageVolumes: { path: "/inventory/storage-volumes", search: true, facets: ["node"] },
  isoImages: { path: "/inventory/iso-images", search: true, facets: ["node"] },
  networkDetail: { path: "/inventory/networks/[name]", search: false, facets: [], reserved: ["host", "project"] },
} as const;

type TargetName = keyof typeof LINK_TARGETS;
type FacetKey<T extends TargetName> = (typeof LINK_TARGETS)[T]["facets"][number];
type ReservedKey<T extends TargetName> = (typeof LINK_TARGETS)[T] extends {
  reserved: readonly (infer R extends string)[];
}
  ? R
  : never;

type FacetValue = string | readonly string[] | null | undefined;

interface Parts<T extends TargetName> {
  q?: string | null;
  facets?: Partial<Record<FacetKey<T>, FacetValue>>;
  reserved?: Partial<Record<ReservedKey<T>, string | null | undefined>>;
  /** Substituted for `[name]` in the path, encoded. */
  segment?: string;
}

function build<T extends TargetName>(target: T, parts: Parts<T>): string {
  const spec = LINK_TARGETS[target];
  const path = parts.segment !== undefined
    ? spec.path.replace("[name]", encodeURIComponent(parts.segment))
    : spec.path;
  const params = new URLSearchParams();
  // note: `q` is the raw substring the search box holds, so it takes URLSearchParams' own
  // encoding. Facet values go through serialiseFacet, which is what the filter hook writes
  // and parseFacet undoes: a value with a comma or a percent sign survives only that way.
  if (parts.q) params.set("q", parts.q);
  for (const [key, raw] of Object.entries(parts.facets ?? {})) {
    const values = (Array.isArray(raw) ? raw : [raw]).filter(
      (value): value is string => typeof value === "string" && value !== "",
    );
    if (values.length) params.set(key, serialiseFacet(values));
  }
  for (const [key, value] of Object.entries(parts.reserved ?? {})) {
    if (typeof value === "string" && value !== "") params.set(key, value);
  }
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

export interface InstancesLink {
  q?: string | null;
  /** The registered node id, what `VMInfo.host` carries. Never a display name. */
  host?: string | null;
  project?: string | null;
}

export interface NodeSearchLink {
  q?: string | null;
  /** The registered node id the row was read from. */
  node?: string | null;
}

export interface WarningsLink {
  tab?: "all" | "certificates" | "nodes";
  /** A tile bucket: expands to every raw severity that tile counted. */
  severity?: SeverityBucket | null;
  /** Platform tab: the registered node (`warningNodeValue`). */
  node?: string | null;
  /** Certificates or nodes tab: the row's own value (`warningClusterValue`). */
  cluster?: string | null;
}

export const inventoryLinks = {
  instances: (link: InstancesLink) =>
    build("instances", { q: link.q, facets: { host: link.host, project: link.project } }),

  // note: the only cell-level caller is components/ui/node-link.tsx, which owns the Partner
  // gate (Clusters is Partner-tier). scripts/checks/check-node-link.mjs refuses any other.
  clusters: (link: { q?: string | null }) => build("clusters", { q: link.q }),

  certificates: (link: { q?: string | null; cluster?: string | null }) =>
    build("certificates", { q: link.q, facets: { cluster: link.cluster } }),

  members: (clusterId: string) => build("members", { reserved: { node: clusterId } }),

  warnings: (link: WarningsLink = {}) => {
    const tab = link.tab && link.tab !== "all" ? link.tab : undefined;
    const clusterKey = tab === "certificates" ? "certificates.cluster" : "nodes.cluster";
    return build("warnings", {
      reserved: { tab },
      facets: {
        "all.severity": link.severity ? WARNING_SEVERITY_BUCKETS[link.severity] : undefined,
        "all.node": link.node,
        [clusterKey]: tab ? link.cluster : undefined,
      },
    });
  },

  // The project is the image store the row sits in, which a used_by reference names. An
  // instance's base-image link sends none: the image lives under the instance's effective
  // image project, which is `default` for a project that shares the host's store.
  images: (link: NodeSearchLink & { project?: string | null }) =>
    build("images", { q: link.q, facets: { node: link.node, project: link.project } }),

  profiles: (link: NodeSearchLink & { project?: string | null }) =>
    build("profiles", { q: link.q, facets: { node: link.node, project: link.project } }),

  operations: (link: NodeSearchLink) =>
    build("operations", { q: link.q, facets: { node: link.node } }),

  networkAcls: (link: NodeSearchLink) =>
    build("networkAcls", { q: link.q, facets: { node: link.node } }),

  storagePools: (link: NodeSearchLink) =>
    build("storagePools", { q: link.q, facets: { node: link.node } }),

  storageVolumes: (link: NodeSearchLink) =>
    build("storageVolumes", { q: link.q, facets: { node: link.node } }),

  isoImages: (link: NodeSearchLink) =>
    build("isoImages", { q: link.q, facets: { node: link.node } }),

  /**
   * The one record page. `project` is sent whenever it is known and not the default: the
   * page reads the default project without it, and a network held by another project then
   * fails to load.
   */
  networkDetail: (name: string, host: string, project?: string | null) =>
    build("networkDetail", {
      segment: name,
      reserved: { host, project: project && project !== "default" ? project : undefined },
    }),
} as const;
