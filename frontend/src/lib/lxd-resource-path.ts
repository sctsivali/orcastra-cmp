/**
 * The LXD API path for a resource, qualified by the project that holds it.
 *
 * LXD auth-group permissions are granted against a path, and a project-scoped resource is only
 * addressable with its project on the query string. Two projects can each hold a `default`
 * profile or a `br0` network, so a bare path is ambiguous the moment the console can see past
 * one project.
 *
 * The edit dialog already built paths this way against a `project` field the API had never
 * sent, while the create dialog built bare ones. Both now share this.
 */

export const DEFAULT_PROJECT = "default";

export function lxdResourcePath(
  collection: string,
  name: string,
  project?: string | null,
): string {
  const base = `/1.0/${collection}/${name}`;
  if (!project || project === DEFAULT_PROJECT) return base;
  return `${base}?project=${encodeURIComponent(project)}`;
}
