import type { QueryClient, QueryKey } from "@tanstack/react-query";

/** The point at which a successful write is authoritative. */
export type MutationCompletion = "response" | "terminal-operation";

const QUERY_FAMILY_KEYS = {
  instances: [["instances"], ["vms"]],
  instanceDetails: [["instance"], ["vm"], ["instanceSpecification"], ["instanceSpecifications"]],
  instanceMetrics: [["instanceMetrics"]],
  instanceOperations: [["instanceOperation"]],
  monitoring: [["monitoring"]],
  ipam: [["ipam"]],
  projectUsage: [["projectUsage"]],
  images: [["images"]],
  nodes: [["nodes"]],
  nodePicker: [["nodeNames"], ["nodeAvailability"]],
  nodeResources: [["nodeResources"], ["nodeGpus"], ["memberResources"]],
  projects: [["projects"]],
  tags: [["tagCategories"], ["tags"]],
  tagStats: [["tags", "stats"]],
  resourceTags: [["tags", "resource"]],
  tagVisibility: [["tags", "visibility"]],
  storagePools: [["storagePools"], ["storagePoolList"]],
  storagePoolDetails: [["storagePool"], ["storagePoolResources"]],
  storageVolumes: [["storageVolumes"]],
  storageVolumeDetails: [["storageVolume"]],
  networks: [["networks"]],
  networkDetails: [["network"], ["networkState"]],
  networkAcls: [["networkACLs"]],
  networkAclDetails: [["networkACL"]],
  profiles: [["profiles"]],
  profileDetails: [["profile"]],
  identities: [["identities"]],
  identityDetails: [["identity"]],
  groups: [["groups"]],
  groupDetails: [["group"]],
  clusters: [["clusters"]],
  clusterDetails: [["cluster"]],
  clusterMembers: [["clusterMembers"], ["clusterMember"]],
  certificates: [["certificates"], ["certificate"]],
  regions: [["regions"], ["facilities"]],
  snapshots: [["instanceSnapshots"], ["fleetSnapshots"]],
  snapshotSchedules: [["snapshotSchedules"]],
  warnings: [["warnings"], ["systemWarnings"]],
  server: [["serverInfo"], ["serverResources"]],
  breakGlass: [["break-glass"], ["break-glass-grants"]],
  integrations: [["integrations"]],
  extensions: [["extensions"]],
  myKeys: [["my-keys"]],
  organizations: [["organizations"], ["organization-preferences"], ["integration-organizations"]],
  personalization: [["user-preferences"], ["organization-preferences"]],
  tenantAccess: [["tenant-access-policies"], ["tenant-access-assignments"]],
  userGroups: [["user-groups"]],
  tasks: [["tasks"], ["task"], ["cluster-log"]],
  migrations: [["migrations"]],
  // note: not ["vault-status"]. A plain re-read there can land on a worker still holding "sealed"
  // for ten seconds; an unseal hands the notice a fresh read instead (announceUnsealed).
  hostHealth: [["host-health"], ["vault-seal-status"]],
} as const satisfies Record<string, readonly QueryKey[]>;

export type QueryFamily = keyof typeof QUERY_FAMILY_KEYS;

export interface MutationImpact {
  completion: MutationCompletion;
  queryFamilies: readonly QueryFamily[];
  /** API member expressions covered by the contract and its structural guard. */
  apiCalls: readonly string[];
}

function defineMutationImpacts<const T extends Record<string, MutationImpact>>(impacts: T): T {
  return impacts;
}

/**
 * One auditable contract for frontend writes. Keep call expressions here even when a mutation
 * has a specialised optimistic patch: the terminal reconciliation still uses these families.
 */
export const MUTATION_IMPACTS = defineMutationImpacts({
  "migration.plan": {
    completion: "response",
    queryFamilies: ["migrations"],
    apiCalls: ["migrationsAPI.createPlan", "migrationsAPI.updatePlan"],
  },
  "migration.job": {
    completion: "response",
    queryFamilies: ["migrations", "tasks", "instances", "instanceDetails", "monitoring", "ipam", "projectUsage", "projects", "profiles", "networks", "networkAcls", "storagePools", "storageVolumes", "images", "snapshots", "snapshotSchedules", "nodes", "nodeResources", "regions"],
    apiCalls: ["migrationsAPI.executePlan", "migrationsAPI.action"],
  },
  "instance.create": {
    completion: "terminal-operation",
    queryFamilies: ["instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "resourceTags", "tagStats", "nodes", "nodeResources", "regions", "profiles", "profileDetails", "networks", "networkDetails", "storagePools", "storagePoolDetails", "storageVolumes", "storageVolumeDetails"],
    apiCalls: ["api.instances.create"],
  },
  "instance.create-legacy": {
    completion: "response",
    queryFamilies: ["instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "resourceTags", "tagStats", "nodes", "nodeResources", "regions", "profiles", "profileDetails", "networks", "networkDetails", "storagePools", "storagePoolDetails", "storageVolumes", "storageVolumeDetails"],
    apiCalls: ["vmAPI.create"],
  },
  "instance.specification": {
    completion: "terminal-operation",
    queryFamilies: ["instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "nodes", "nodeResources", "regions", "profiles", "networks", "storagePools", "storageVolumes"],
    apiCalls: ["api.instances.updateSpecification"],
  },
  "instance.rebuild": {
    completion: "terminal-operation",
    queryFamilies: ["instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "nodes", "nodeResources", "regions", "storagePools", "storagePoolDetails", "storageVolumes", "networks", "profiles"],
    apiCalls: ["api.instances.rebuild"],
  },
  "instance.state": {
    completion: "response",
    queryFamilies: ["instances", "instanceDetails", "instanceMetrics", "monitoring", "nodes", "nodeResources", "regions"],
    apiCalls: [
      "api.vms.start", "api.vms.stop", "api.vms.restart",
      "vmAPI.start", "vmAPI.stop", "vmAPI.restart",
    ],
  },
  "instance.delete": {
    completion: "response",
    queryFamilies: ["instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "resourceTags", "tagStats", "nodes", "nodeResources", "regions", "snapshots", "profiles", "profileDetails", "networks", "networkDetails", "storagePools", "storagePoolDetails", "storageVolumes", "storageVolumeDetails"],
    apiCalls: ["api.vms.delete", "vmAPI.delete"],
  },
  "instance.tags": {
    completion: "response",
    queryFamilies: ["instances", "instanceDetails", "resourceTags", "tagStats"],
    apiCalls: ["tagAPI.updateInstanceTags"],
  },
  "instance.nic": {
    completion: "response",
    queryFamilies: ["instances", "instanceDetails", "ipam", "networks", "networkDetails"],
    apiCalls: ["api.instanceDevices.setNicAddress"],
  },
  "instance.media": {
    completion: "response",
    queryFamilies: ["instances", "instanceDetails", "storageVolumes", "storageVolumeDetails"],
    apiCalls: ["consoleAPI.attachISO", "consoleAPI.ejectISO"],
  },
  "instance.boot-order": {
    completion: "response",
    queryFamilies: ["instances", "instanceDetails"],
    apiCalls: ["consoleAPI.updateStartupOrder"],
  },
  "image.delete": {
    completion: "response",
    queryFamilies: ["images", "instances", "nodes", "nodeResources", "regions", "storagePoolDetails"],
    apiCalls: ["imageAPI.delete"],
  },
  "image.upload": {
    completion: "terminal-operation",
    queryFamilies: ["images", "storagePoolDetails", "nodes", "nodeResources", "regions"],
    apiCalls: ["api.imageUploads.finalize"],
  },
  "node.update": {
    completion: "response",
    queryFamilies: ["nodes", "nodePicker", "nodeResources", "regions", "clusters", "clusterDetails", "resourceTags", "tagStats"],
    apiCalls: ["nodeAPI.updateDisplayName", "tagAPI.updateNodeTags"],
  },
  "node.cache": {
    completion: "response",
    queryFamilies: ["nodes", "nodePicker", "nodeResources", "instances", "projects", "regions", "clusterMembers"],
    apiCalls: ["nodeAPI.invalidateCache", "nodeAPI.invalidateAllCache", "api.system.clearCache"],
  },
  "project.write": {
    completion: "response",
    queryFamilies: ["projects", "projectUsage", "instances", "images", "profiles", "networks", "storageVolumes", "resourceTags", "tagStats"],
    apiCalls: ["projectAPI.create", "projectAPI.update", "projectAPI.delete", "tagAPI.updateProjectTags"],
  },
  "tag.catalogue": {
    completion: "response",
    queryFamilies: ["tags", "tagStats", "resourceTags", "instances", "nodes", "projects"],
    apiCalls: [
      "tagAPI.createCategory", "tagAPI.updateCategory", "tagAPI.createTag", "tagAPI.updateTag",
      "tagAPI.deleteTag", "tagAPI.initCategories",
    ],
  },
  "tag.resource": {
    completion: "response",
    queryFamilies: ["resourceTags", "tagStats", "instances", "instanceDetails", "nodes", "projects"],
    apiCalls: ["tagAPI.setResourceTags", "tagAPI.addResourceTags", "tagAPI.removeResourceTag"],
  },
  "tag.visibility": {
    completion: "response",
    queryFamilies: ["tagVisibility", "tags"],
    apiCalls: ["tagAPI.updateUserVisibility"],
  },
  "storage.pool": {
    completion: "response",
    queryFamilies: ["storagePools", "storagePoolDetails", "nodes", "nodeResources", "regions", "projects"],
    apiCalls: ["api.storagePools.create", "api.storagePools.update", "api.storagePools.delete"],
  },
  "storage.volume": {
    completion: "response",
    queryFamilies: ["storageVolumes", "storageVolumeDetails", "storagePools", "storagePoolDetails", "nodes", "nodeResources", "regions", "instances"],
    apiCalls: ["api.storageVolumes.create", "api.storageVolumes.update", "api.storageVolumes.delete"],
  },
  "iso.upload": {
    completion: "terminal-operation",
    queryFamilies: ["storageVolumes", "storagePools", "storagePoolDetails", "nodes", "nodeResources", "regions"],
    apiCalls: ["api.isoUploads.finalize"],
  },
  "network.write": {
    completion: "response",
    queryFamilies: ["networks", "networkDetails", "ipam", "instances", "profiles"],
    apiCalls: ["api.networks.create", "api.networks.update", "api.networks.delete"],
  },
  "network-acl.write": {
    completion: "response",
    queryFamilies: ["networkAcls", "networkAclDetails", "networks", "profileDetails"],
    apiCalls: ["api.networkACLs.create", "api.networkACLs.update", "api.networkACLs.delete"],
  },
  "profile.write": {
    completion: "response",
    queryFamilies: ["profiles", "profileDetails", "instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "networkDetails", "networks", "storagePools", "storageVolumes", "nodes", "nodeResources", "regions"],
    apiCalls: ["api.profiles.create", "api.profiles.update", "api.profiles.delete"],
  },
  "identity.write": {
    completion: "response",
    queryFamilies: ["identities", "identityDetails", "groups", "groupDetails"],
    apiCalls: ["api.identities.delete"],
  },
  "group.write": {
    completion: "response",
    queryFamilies: ["groups", "groupDetails", "identities", "identityDetails"],
    apiCalls: [
      "api.groups.create", "api.groups.update", "api.groups.delete",
      "api.groups.addIdentity", "api.groups.removeIdentity",
    ],
  },
  "cluster.register": {
    completion: "response",
    queryFamilies: ["clusters", "clusterDetails", "clusterMembers", "nodes", "nodePicker", "nodeResources", "instances", "instanceDetails", "monitoring", "ipam", "projects", "images", "storagePools", "storagePoolDetails", "storageVolumes", "networks", "networkDetails", "networkAcls", "profiles", "profileDetails", "snapshots", "snapshotSchedules", "certificates", "regions", "warnings", "organizations", "identities", "identityDetails", "groups", "groupDetails", "integrations"],
    apiCalls: ["api.clusters.register"],
  },
  "cluster.auth-check": {
    completion: "response",
    // a check that turns a pending cluster trusted makes everything on it readable at once
    queryFamilies: ["clusters", "clusterDetails", "clusterMembers", "nodes", "nodePicker", "nodeResources", "instances", "instanceDetails", "monitoring", "ipam", "projects", "images", "storagePools", "storagePoolDetails", "storageVolumes", "networks", "networkDetails", "networkAcls", "profiles", "profileDetails", "snapshots", "certificates", "regions", "warnings"],
    apiCalls: ["api.clusters.checkAuth"],
  },
  "cluster.delete": {
    completion: "response",
    queryFamilies: ["clusters", "clusterDetails", "clusterMembers", "nodes", "nodePicker", "nodeResources", "instances", "instanceDetails", "monitoring", "ipam", "projects", "images", "storagePools", "storagePoolDetails", "storageVolumes", "networks", "networkDetails", "networkAcls", "profiles", "profileDetails", "snapshots", "snapshotSchedules", "certificates", "regions", "warnings", "organizations", "identities", "identityDetails", "groups", "groupDetails", "integrations"],
    apiCalls: ["api.clusters.delete"],
  },
  "cluster.certificate": {
    completion: "response",
    queryFamilies: ["clusters", "clusterDetails", "nodes", "nodePicker", "certificates", "warnings"],
    apiCalls: [
      "api.clusters.regenerateCert", "api.certificates.initiateRenewal",
      "api.certificates.verifyRenewal", "api.certificates.cancelPending",
      "api.certificates.cleanupPending", "api.certificates.removeTrust",
    ],
  },
  "cluster.location": {
    completion: "response",
    queryFamilies: ["clusters", "clusterDetails", "nodes", "regions"],
    apiCalls: ["api.clusters.updateLocation"],
  },
  "snapshot.write": {
    completion: "terminal-operation",
    queryFamilies: ["snapshots", "snapshotSchedules", "instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "profiles", "networks", "storagePools", "storagePoolDetails", "storageVolumes", "nodes", "nodeResources", "regions"],
    apiCalls: ["api.snapshots.create", "api.snapshots.remove", "api.snapshots.restore"],
  },
  "snapshot.schedule": {
    completion: "response",
    queryFamilies: ["snapshotSchedules", "snapshots"],
    apiCalls: [
      "api.snapshotSchedules.create", "api.snapshotSchedules.update", "api.snapshotSchedules.pause",
      "api.snapshotSchedules.resume", "api.snapshotSchedules.remove",
    ],
  },
  "snapshot.schedule-run": {
    completion: "response",
    queryFamilies: ["snapshotSchedules", "snapshots", "instances", "instanceDetails", "instanceMetrics", "monitoring", "ipam", "projectUsage", "profiles", "networks", "storagePools", "storagePoolDetails", "storageVolumes", "nodes", "nodeResources", "regions"],
    apiCalls: ["api.snapshotSchedules.runNow"],
  },
  "break-glass.write": {
    completion: "response",
    queryFamilies: ["breakGlass", "instances", "instanceDetails"],
    apiCalls: ["api.breakGlass.createGrant", "api.breakGlass.revokeGrant"],
  },
  "integration.write": {
    completion: "response",
    queryFamilies: ["integrations", "organizations"],
    apiCalls: [
      "api.integrations.createAPIKey", "api.integrations.updateAPIKeyPermissions",
      "api.integrations.updateAPIKeyCapabilities",
      "api.integrations.revokeAPIKey", "api.integrations.deleteAPIKey",
      "api.integrations.rotateAPIKeySecret",
      "api.integrations.addUserMapping", "api.integrations.deleteUserMapping",
      "api.integrations.refreshOrganizations", "api.integrations.transferOrganizationOwner",
      "api.integrations.addOrganizationOwner", "api.integrations.removeOrganizationOwner",
      "api.integrations.setOrganizationPrimary",
    ],
  },
  "extension.write": {
    completion: "response",
    queryFamilies: ["extensions"],
    apiCalls: [
      "extensionsAPI.install", "extensionsAPI.create", "extensionsAPI.update",
      "extensionsAPI.setStatus", "extensionsAPI.publishVersion",
      "extensionsAPI.withdraw", "extensionsAPI.suspend", "extensionsAPI.resume",
      "extensionsAPI.uninstall", "extensionsAPI.updateSettings", "extensionsAPI.rotateCredential",
      "extensionsAPI.redeliver", "extensionsAPI.createSubscription",
      "extensionsAPI.verifySubscription", "extensionsAPI.rotateSubscriptionSecret",
      "extensionsAPI.deleteSubscription",
    ],
  },
  "key.write": {
    completion: "response",
    queryFamilies: ["myKeys"],
    apiCalls: ["api.myKeys.create", "api.myKeys.delete"],
  },
  "organization.preferences": {
    completion: "response",
    queryFamilies: ["organizations"],
    apiCalls: ["api.organizations.updatePreferences", "api.organizations.deletePreferences", "api.organizations.desktopControl.set"],
  },
  "organization.write": {
    completion: "response",
    queryFamilies: ["organizations", "clusters", "clusterDetails", "clusterMembers", "nodes", "nodePicker", "instances", "projects", "identities", "groups", "tenantAccess", "personalization", "integrations"],
    apiCalls: [
      "api.organizations.create", "api.organizations.update", "api.organizations.delete",
      "api.organizations.addMember", "api.organizations.updateMember",
      "api.organizations.removeMember", "api.organizations.bindCluster",
      "api.organizations.unbindCluster",
    ],
  },
  "warning.write": {
    completion: "response",
    queryFamilies: ["warnings"],
    apiCalls: ["api.warnings.delete"],
  },
  "server.configuration": {
    completion: "response",
    queryFamilies: ["server", "nodes", "nodePicker", "nodeResources", "regions"],
    apiCalls: ["api.server.updateConfig"],
  },
  "personalization.write": {
    completion: "response",
    queryFamilies: ["personalization"],
    apiCalls: ["personalizationAPI.update", "personalizationAPI.delete"],
  },
  "tenant-access.write": {
    completion: "response",
    queryFamilies: ["tenantAccess", "instances", "projects", "nodes"],
    apiCalls: [
      "api.tenantAccess.createAssignment", "api.tenantAccess.deleteAssignment",
      "api.tenantAccess.createPolicy", "api.tenantAccess.updatePolicy", "api.tenantAccess.deletePolicy",
    ],
  },
  "user-group.write": {
    completion: "response",
    queryFamilies: ["userGroups", "groups", "identities"],
    apiCalls: ["api.userGroups.assign", "api.userGroups.remove"],
  },
  "task.cancel": {
    completion: "terminal-operation",
    queryFamilies: ["tasks", "instances", "instanceDetails", "instanceMetrics", "instanceOperations", "monitoring", "ipam", "projectUsage", "images", "nodes", "nodePicker", "nodeResources", "projects", "tags", "tagStats", "resourceTags", "storagePools", "storagePoolDetails", "storageVolumes", "storageVolumeDetails", "networks", "networkDetails", "networkAcls", "networkAclDetails", "profiles", "profileDetails", "clusters", "clusterDetails", "clusterMembers", "regions", "snapshots", "snapshotSchedules", "warnings"],
    apiCalls: ["tasksAPI.cancel"],
  },
  "task.session": {
    completion: "response",
    queryFamilies: ["tasks", "instances", "instanceDetails"],
    apiCalls: [
      "tasksAPI.killConsoleSession",
      "tasksAPI.killAllConsoleSessions",
      "tasksAPI.killAllConsoleSessionsOnUnload",
    ],
  },
  "file-transfer.push": {
    completion: "response",
    queryFamilies: ["instanceDetails", "tasks"],
    apiCalls: ["fileTransferAPI.push"],
  },
  // The answer is Vault's: progress moves at once, and an unseal hands the notice a fresh status.
  "vault.unseal": {
    completion: "response",
    queryFamilies: ["hostHealth", "tasks"],
    apiCalls: ["hostHealthAPI.unseal"],
  },
} as const);

export type MutationImpactId = keyof typeof MUTATION_IMPACTS;

/** Narrow exceptions: read-only command endpoints and streaming upload protocols. */
export const MUTATION_CALL_EXCEPTIONS = {
  "api.instances.preflightCreate": "read-only validation",
  "api.instances.preflightSpecification": "read-only validation",
  "api.integrations.validate": "read-only validation",
  "api.tags.classifyTags": "read-only tag classification command",
  "api.tags.validateTags": "read-only tag validation command",
  "personalizationAPI.proxyImage": "read-only guarded image fetch",
  "api.imageUploads.create": "non-terminal streaming upload stage",
  "api.imageUploads.patchChunk": "non-terminal streaming upload stage",
  "api.imageUploads.abort": "streaming upload cancellation",
  "api.isoUploads.create": "non-terminal streaming upload stage",
  "api.isoUploads.patchChunk": "non-terminal streaming upload stage",
  "api.isoUploads.abort": "streaming upload cancellation",
} as const;

export interface ReconcileMutationOptions {
  /** Additional exact keys owned by the caller, such as one resource-tag row. */
  queryKeys?: readonly QueryKey[];
  /** Active observers refetch by default; use `none` when an authoritative patch follows. */
  refetchType?: "active" | "all" | "inactive" | "none";
}

export async function reconcileMutationImpact(
  queryClient: QueryClient,
  impactId: MutationImpactId,
  options: ReconcileMutationOptions = {},
): Promise<void> {
  const impact = MUTATION_IMPACTS[impactId];
  const refetchType = options.refetchType ?? "active";
  const keys: QueryKey[] = [];
  for (const family of impact.queryFamilies) {
    keys.push(...(QUERY_FAMILY_KEYS[family] as readonly QueryKey[]));
  }
  const unique = new Map<string, QueryKey>();
  for (const key of [...keys, ...(options.queryKeys ?? [])]) {
    unique.set(JSON.stringify(key), key);
  }
  await Promise.all(
    [...unique.values()].map((queryKey) =>
      queryClient.invalidateQueries({ queryKey, refetchType }),
    ),
  );
}

let externalMutationQueryClient: QueryClient | null = null;

export interface ExternalMutationContext {
  readonly queryClient: QueryClient;
}

/**
 * Register the active actor's browser query client for writes initiated outside React hooks.
 *
 * Zustand stores such as the break-glass gate cannot call `useQueryClient`. The provider replaces
 * the complete QueryClient at every actor boundary, and the identity-checked disposer prevents an
 * old provider effect from clearing a newer actor's client.
 */
export function registerExternalMutationQueryClient(queryClient: QueryClient): () => void {
  externalMutationQueryClient = queryClient;
  return () => {
    if (externalMutationQueryClient === queryClient) externalMutationQueryClient = null;
  };
}

/** Capture the actor-owned client before starting an out-of-tree mutation. */
export function captureExternalMutationContext(): ExternalMutationContext | null {
  return externalMutationQueryClient
    ? { queryClient: externalMutationQueryClient }
    : null;
}

/** Reconcile a registered write that originates outside React Query's hook tree. */
export async function reconcileExternalMutation(
  impactId: MutationImpactId,
  options: ReconcileMutationOptions = {},
  context?: ExternalMutationContext | null,
): Promise<void> {
  const queryClient = context?.queryClient ?? externalMutationQueryClient;
  if (!queryClient) return;
  // A context captured by A may never invalidate B's replacement QueryClient. Clearing A's
  // retired client is handled at the auth boundary; this completion is intentionally discarded.
  if (context && externalMutationQueryClient !== queryClient) return;
  await reconcileMutationImpact(queryClient, impactId, options);
}
