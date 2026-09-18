import { EMPTY_VALUE } from "@/components/ui/table-text";
import { lxdResourcePath } from "@/lib/lxd-resource-path";

/**
 * The resources a group permission can name, built the same way in the create and the edit
 * dialog. Every path carries the project that holds the record (`lxdResourcePath` appends
 * `?project=` unless it is default), so two rows called "default" or two instances called
 * "web" in two projects are two resources, and an image is addressed in the store that holds it.
 */
export interface PermissionResource {
  name: string;
  path: string;
  /** The project that holds it, or the image store it sits in. */
  project?: string;
}

export interface PermissionResourceData {
  projects?: unknown;
  instances?: unknown;
  images?: unknown;
  profiles?: unknown;
  networks?: unknown;
  pools?: unknown;
}

const rows = (data: unknown, key: string): any[] => ((data as any)?.[key] as any[]) || [];

// The image label is what an operator typed (alias) or what the registry wrote (description),
// never re-cased; the fingerprint is the last resort.
function imageName(img: any): string {
  return (
    img.description || img.properties?.description || img.aliases?.find(Boolean) || img.fingerprint || EMPTY_VALUE
  );
}

export function permissionResources(entityType: string, data: PermissionResourceData): PermissionResource[] {
  switch (entityType) {
    case "project":
      return rows(data.projects, "projects").map((p) => ({ name: p.name, path: `/1.0/projects/${p.name}` }));
    case "instance":
      return rows(data.instances, "vms").map((i) => ({
        name: i.name,
        path: lxdResourcePath("instances", i.name, i.project),
        project: i.project,
      }));
    case "image": {
      // The stores read answers one response; the fleet read answers one per host.
      const images = Array.isArray(data.images)
        ? data.images.flatMap((host: any) => host?.images || [])
        : rows(data.images, "images");
      return images.map((img: any) => ({
        name: imageName(img),
        path: lxdResourcePath("images", img.fingerprint, img.project),
        project: img.project,
      }));
    }
    case "profile":
      return rows(data.profiles, "profiles").map((p) => ({
        name: p.name,
        path: lxdResourcePath("profiles", p.name, p.project),
        project: p.project,
      }));
    case "network":
      return rows(data.networks, "networks").map((n) => ({
        name: n.name,
        path: lxdResourcePath("networks", n.name, n.project),
        project: n.project,
      }));
    case "storage_pool":
      return rows(data.pools, "pools").map((s) => ({ name: s.name, path: `/1.0/storage-pools/${s.name}` }));
    default:
      return [];
  }
}

/** What to print beside a resource name: the store for an image, the project for the rest. */
export function resourceQualifier(entityType: string, project?: string): string | null {
  if (!project || project === "default") return null;
  return entityType === "image" ? `store: ${project}` : project;
}
