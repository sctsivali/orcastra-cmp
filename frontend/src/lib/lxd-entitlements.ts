// LXD permission vocabulary, shared by the group dialogs and anywhere an entitlement or an
// entity type is displayed.
//
// note: these three helpers were duplicated between create-group-dialog.tsx and
// edit-group-dialog.tsx, and the copies had drifted. The create dialog's label map was
// missing the `user` entry while its valid-entitlement list still offered `user` for
// instances, so that option rendered as the raw string "user" in one dialog and as
// "User (Console/exec access)" in the other. Extracting them resolves that to the complete
// map rather than the lossy one.

const RESOURCE_BASE_PATHS: Record<string, string> = {
  project: "/1.0/projects",
  instance: "/1.0/instances",
  image: "/1.0/images",
  profile: "/1.0/profiles",
  network: "/1.0/networks",
  storage_pool: "/1.0/storage-pools",
  storage_volume: "/1.0/storage-volumes",
};

// Valid entitlements per entity type, from the LXD documentation and confirmed against the
// API's own validation.
// important: can_view is NOT valid for storage_pool, and project cannot take can_edit
// because it requires can_view and the pair cannot be set atomically.
const VALID_ENTITLEMENTS: Record<string, string[]> = {
  server: ["admin"],
  project: ["can_view", "operator"],
  instance: ["can_view", "can_edit", "operator", "user"],
  image: ["can_view", "can_edit"],
  profile: ["can_view", "can_edit"],
  network: ["can_view", "can_edit"],
  network_acl: ["can_view", "can_edit"],
  storage_pool: ["can_edit"],
  storage_volume: ["can_view", "can_edit"],
};

const ENTITLEMENT_LABELS: Record<string, string> = {
  can_view: "Can View (Read-only)",
  can_edit: "Can Edit (Modify)",
  can_manage: "Can Manage (Full control)",
  operator: "Operator (Operations)",
  admin: "Admin (Full access)",
  user: "User (Console/exec access)",
};

export function getResourceBasePath(entityType: string): string {
  return RESOURCE_BASE_PATHS[entityType] || "/1.0";
}

export function getValidEntitlements(entityType: string): string[] {
  return VALID_ENTITLEMENTS[entityType] || ["can_view", "can_edit"];
}

export function getEntitlementLabel(entitlement: string): string {
  return ENTITLEMENT_LABELS[entitlement] || entitlement;
}
