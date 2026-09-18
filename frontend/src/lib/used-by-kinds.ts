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
// note: the icons are the sidebar's own (components/sidebar/nav-config.tsx), so a network
// listed inside a dialog wears the same glyph as the nav entry it links to. Picking a second
// set here would teach the operator two vocabularies for one set of things.

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
 * A filtered list view. Honest about being a search preset rather than a record link.
 *
 * note: the node travels too. A profile named `default` exists on every cluster, and the
 * dialog knows which cluster the used_by list came from, so the list opens narrowed to it.
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

// important: every href below points at a route that exists today. Do not add an entry for a
// destination you intend to build later: a link that lands nowhere is worse than plain text,
// and `projects` is deliberately absent because there is no /inventory/projects route.
const KINDS: Record<string, KindDescriptor> = {
  instances: { label: "Instance", icon: Box, href: instanceHref },
  profiles: { label: "Profile", icon: SlidersHorizontal, href: searchOn(inventoryLinks.profiles) },
  networks: {
    label: "Network",
    icon: Network,
    // The one true detail route in the app. It renders "Missing host parameter" without a
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
  images: { label: "Image", icon: ImageIcon, href: searchOn(inventoryLinks.images) },
  custom: { label: "Volume", icon: HardDrive, href: searchOn(inventoryLinks.storageVolumes) },
  block: { label: "Block Volume", icon: HardDrive, href: searchOn(inventoryLinks.storageVolumes) },
  iso: { label: "ISO Image", icon: Disc, href: searchOn(inventoryLinks.isoImages) },
  // An instance-backed volume IS an instance's own disk, so the useful destination is the
  // instance, not the volume list.
  container: { label: "Instance", icon: Box, href: instanceHref },
  "virtual-machine": { label: "Instance", icon: Box, href: instanceHref },
  projects: { label: "Project", icon: FolderOpen, href: null },
};

const UNKNOWN: KindDescriptor = { label: "Unknown", icon: HelpCircle, href: null };

export function referenceKind(reference: LxdReference): KindDescriptor {
  const kind = reference.kind;
  if (!kind) return UNKNOWN;
  // note: KINDS carries the volume-type words (custom, block, iso, container, virtual-machine)
  // as first-class entries. parseLxdReference's fallthrough takes the last two path segments,
  // and a volume path ends `/volumes/<type>/<name>`, so those words are what land in `kind`.
  // Normalising them here rather than in the parser is deliberate: that function is a faithful
  // port of the backend's tenant-visibility parser and must not drift for a cosmetic reason.
  const known = KINDS[kind];
  if (known) return known;
  // A degenerate reference such as "/1.0/instances" leaves the API version in `kind`. Naming it
  // "1.0" as though it were a resource type would be worse than admitting we do not know.
  if (kind === "1.0") return UNKNOWN;
  // Anything else is a path segment, which IS a lowercase API enum, so formatLabel is right.
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
 * Count references by DISPLAY kind, descending by count then label.
 *
 * note: keyed on the label rather than the raw kind on purpose. A custom volume parses as
 * "custom" and a block volume as "block", and an operator reading "3 volumes" does not want
 * those split into two rows over a path-segment distinction the UI does not otherwise make.
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
