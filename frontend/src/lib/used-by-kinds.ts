import {
  Box,
  Database,
  Disc,
  FolderOpen,
  HardDrive,
  HelpCircle,
  Image as ImageIcon,
  Network,
  Shield,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";
import { formatLabel } from "@/lib/utils";
import type { LxdReference } from "@/lib/used-by";
import { inventoryLinks, type NodeSearchLink } from "@/lib/inventory-links";

// Display vocabulary for the resource kinds an LXD used_by reference can name.
//
// The icons are the sidebar's own (components/sidebar/nav-config.tsx), so a network listed
// inside a dialog wears the same glyph as the nav entry it links to.

export interface KindDescriptor {
  /** Human label for the kind, ex; "Instance". */
  label: string;
  icon: LucideIcon;
  /**
   * Where this reference can be opened, or null when nothing in the app shows it.
   * `node` is the cluster the used_by list came from; a reference does not carry one.
   */
  href: ((reference: LxdReference, node?: string) => string) | null;
}

/**
 * A filtered list view: a search preset rather than a record link.
 *
 * The node travels too. A profile named `default` exists on every cluster, so the list opens
 * narrowed to the cluster the used_by list came from.
 */
function searchOn(target: (link: NodeSearchLink) => string) {
  return (reference: LxdReference, node?: string) =>
    target({ q: reference.name ?? reference.raw, node });
}

// The instances table facets on project and host, so send all three coordinates rather than a
// bare search that can land on an empty list.
function instanceHref(reference: LxdReference, node?: string): string {
  return inventoryLinks.instances({
    q: reference.instance ?? reference.name ?? reference.raw,
    project: reference.project,
    host: node,
  });
}

// Every href below must point at a route that exists; a link that lands nowhere is worse than
// plain text. `projects` has no href because there is no /inventory/projects route.
const KINDS: Record<string, KindDescriptor> = {
  instances: { label: "Instance", icon: Box, href: instanceHref },
  profiles: { label: "Profile", icon: SlidersHorizontal, href: searchOn(inventoryLinks.profiles) },
  networks: {
    label: "Network",
    icon: Network,
    // The only detail route among these kinds. It renders "Missing host parameter" without a
    // host, and a reference carries none, so it takes the node the used_by list came from.
    // The project travels with it: without one the page reads the default project and a
    // network held by another project fails to load.
    href: (reference, node) =>
      node
        ? inventoryLinks.networkDetail(reference.name ?? "", node, reference.project)
        : "/inventory/networks",
  },
  "network-acls": { label: "Network ACL", icon: Shield, href: searchOn(inventoryLinks.networkAcls) },
  "storage-pools": { label: "Storage Pool", icon: Database, href: searchOn(inventoryLinks.storagePools) },
  images: {
    label: "Image",
    icon: ImageIcon,
    // A reference names the store that holds the image, and the list facets on it.
    href: (reference, node) =>
      inventoryLinks.images({ q: reference.name ?? reference.raw, node, project: reference.project }),
  },
  custom: { label: "Volume", icon: HardDrive, href: searchOn(inventoryLinks.storageVolumes) },
  block: { label: "Block Volume", icon: HardDrive, href: searchOn(inventoryLinks.storageVolumes) },
  iso: { label: "ISO Image", icon: Disc, href: searchOn(inventoryLinks.isoImages) },
  // An instance-backed volume is the instance's own disk, so it links to the instance rather
  // than the volume list.
  container: { label: "Instance", icon: Box, href: instanceHref },
  "virtual-machine": { label: "Instance", icon: Box, href: instanceHref },
  projects: { label: "Project", icon: FolderOpen, href: null },
};

const UNKNOWN: KindDescriptor = { label: "Unknown", icon: HelpCircle, href: null };

export function referenceKind(reference: LxdReference): KindDescriptor {
  const kind = reference.kind;
  if (!kind) return UNKNOWN;
  // KINDS also carries the volume-type words (custom, block, iso, container, virtual-machine).
  // parseLxdReference's fallthrough takes the last two path segments, and a volume path ends
  // `/volumes/<type>/<name>`, so those words land in `kind`. They are mapped here, not in the
  // parser, which must stay a faithful port of the backend's tenant-visibility parser.
  const known = KINDS[kind];
  if (known) return known;
  // A degenerate reference such as "/1.0/instances" leaves the API version in `kind`.
  if (kind === "1.0") return UNKNOWN;
  // Anything else is a path segment, a lowercase API enum, which formatLabel handles.
  return { label: formatLabel(kind), icon: Box, href: null };
}

export function referenceKindLabel(reference: LxdReference): string {
  return referenceKind(reference).label;
}

export interface KindCount {
  /** The raw kind as parsed, kept so a caller can key on it. */
  kind: string;
  label: string;
  count: number;
}

/**
 * Count references by display kind, descending by count then label.
 *
 * Keyed on the label rather than the raw kind, so `instances`, `container` and
 * `virtual-machine` references count as one Instance row.
 */
export function countsByKind(references: readonly LxdReference[]): readonly KindCount[] {
  const counts = new Map<string, KindCount>();
  for (const reference of references) {
    const { label } = referenceKind(reference);
    const existing = counts.get(label);
    if (existing) existing.count += 1;
    else counts.set(label, { kind: reference.kind ?? "", label, count: 1 });
  }
  return Array.from(counts.values()).sort(
    (a, b) => b.count - a.count || a.label.localeCompare(b.label),
  );
}
