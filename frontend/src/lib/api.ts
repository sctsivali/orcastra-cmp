/**
 * API Client for Orcastra Dashboard Backend
 */

import {
  VMListResponse,
  VMInfo,
  VMCreateRequest,
  VMCreateResponse,
  VMActionRequest,
  VMActionResponse,
  ImageListResponse,
  RemoteImageListResponse,
  NodesStatusResponse,
  NodeResources,
  GpuCardListResponse,
  ProjectListResponse,
  ProjectCreateRequest,
  ProjectCreateResponse,
  InstanceMetrics,
  APIError,
  ClusterRegisterRequest,
  ClusterRegisterResponse,
  ClusterAuthCheckResponse,
  ClusterListResponse,
  ClusterInfo,
  ClusterDeleteResponse,
  ClusterMembersResponse,
  ClusterMember,
  MemberResources,
  VersionInfo,
  SystemSettings,
  SystemResourceUsage,
  GeoLocation,
  ClusterLocationUpdateResponse,
  Organization,
  OrganizationCreateRequest,
  OrganizationUpdateRequest,
  OrganizationMember,
  OrganizationCluster,
  UserGroupEntry,
  UserGroupAssignRequest,
  UserGroupAssignResponse,
  AuthentikSyncStatus,
  PolicyListResponse,
  AccessPolicy,
  PolicyCreateRequest,
  PolicyUpdateRequest,
  AssignmentListResponse,
  PolicyAssignment,
  AssignmentCreateRequest,
  MyAccessResponse,
} from "@/types";

const API_PREFIX = "/api/v1";

// All HTTP API calls use relative URLs — Next.js rewrites proxy them to the backend.
// This avoids cross-origin (CORS) and mixed-content issues entirely.
const API_BASE_URL = "";

// Relative API URL — safe for browser HTTP fetch calls.
// WebSocket connections use ws-url.ts which derives URLs from NEXT_PUBLIC_API_URL.
export const API_URL = API_PREFIX;

/**
 * Token store for Bearer auth.
 * Components call setAccessToken() after session load;
 * fetchAPI() automatically attaches it to all backend requests.
 */
let _accessToken: string | null = null;
let _onTokenRefreshed: (() => void) | null = null;

/**
 * Register a callback fired when the access token is REPLACED with a new value
 * (silent refresh / rotation), so cached query errors from the old token can be
 * refetched. Registered by a component inside QueryClientProvider.
 */
export function onTokenRefreshed(fn: () => void) {
  _onTokenRefreshed = fn;
}

export function setAccessToken(token: string | null | undefined) {
  const next = token ?? null;
  // token rotated (not the initial set) - let listeners recover failed queries
  const rotated = next !== null && _accessToken !== null && next !== _accessToken;
  _accessToken = next;
  if (rotated && _onTokenRefreshed) {
    _onTokenRefreshed();
  }
}

export function getAccessToken(): string | null {
  return _accessToken;
}

/**
 * 401 redirect guard - prevents redirect loops during token refresh.
 * On first 401, triggers a session refresh. Only redirects after
 * multiple consecutive 401s with a cooldown (true session death).
 */
let _401Count = 0;
let _401RedirectPending = false;
const _401_THRESHOLD = 5; // require 5 consecutive 401s before hard redirect
const _401_RESET_MS = 8000; // reset counter after 8s of no 401s
let _401ResetTimer: ReturnType<typeof setTimeout> | null = null;
let _sessionRefreshFn: (() => Promise<void>) | null = null;
let _sessionRefreshInFlight = false;

/**
 * Register a callback for requesting a session refresh (called by auth-provider).
 * When the API layer sees a 401, it calls this to trigger useSession().update()
 * instead of immediately redirecting.
 */
export function onSessionRefreshNeeded(fn: () => Promise<void>) {
  _sessionRefreshFn = fn;
}

function handle401() {
  if (_401RedirectPending) return false; // already redirecting
  _401Count++;

  // Reset counter after a quiet period
  if (_401ResetTimer) clearTimeout(_401ResetTimer);
  _401ResetTimer = setTimeout(() => { _401Count = 0; }, _401_RESET_MS);

  // First few 401s: try to refresh the session token silently
  if (_401Count <= 2 && _sessionRefreshFn && !_sessionRefreshInFlight) {
    _sessionRefreshInFlight = true;
    _sessionRefreshFn().finally(() => {
      _sessionRefreshInFlight = false;
    });
    return true; // signal: refresh triggered, caller may retry
  }

  if (_401Count >= _401_THRESHOLD) {
    _401RedirectPending = true;
    console.warn(`Session expired (${_401Count} consecutive 401s) — redirecting to login`);
    window.location.href = '/api/auth/signin';
  }
  return false;
}

/** Reset 401 counter on successful token set (called by auth-provider). */
export function reset401Counter() {
  _401Count = 0;
  _401RedirectPending = false;
}

// Timeout wrapper for fetch with AbortController
async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeoutMs: number = 45000
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => {
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    return response;
  } catch (error) {
    clearTimeout(timeoutId);
    if (error instanceof Error && error.name === 'AbortError') {
      throw new APIError(`Request timeout after ${timeoutMs / 1000}s - server took too long to respond`, 504, 'Request timeout');
    }
    throw error;
  }
}

/**
 * In-flight GET request deduplication.
 * Prevents duplicate concurrent GET requests to the same endpoint.
 */
const _inflightGets = new Map<string, Promise<unknown>>();

async function fetchAPI<T>(
  endpoint: string,
  options: RequestInit = {},
  timeoutMs: number = 45000,
  _isRetry: boolean = false,
): Promise<T> {
  const url = `${API_BASE_URL}${API_PREFIX}${endpoint}`;
  const method = (options.method || "GET").toUpperCase();

  // Deduplicate concurrent GET requests to the same endpoint
  if (method === "GET" && !_isRetry) {
    const existing = _inflightGets.get(endpoint);
    if (existing) {
      return existing as Promise<T>;
    }
  }

  const request = _fetchAPIInternal<T>(endpoint, url, options, timeoutMs, _isRetry);

  // Track in-flight GET requests for deduplication
  if (method === "GET") {
    _inflightGets.set(endpoint, request);
    request.finally(() => _inflightGets.delete(endpoint));
  }

  return request;
}

async function _fetchAPIInternal<T>(
  endpoint: string,
  url: string,
  options: RequestInit,
  timeoutMs: number,
  _isRetry: boolean,
): Promise<T> {

  try {
    // build headers with auth token
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options.headers as Record<string, string>),
    };
    if (_accessToken) {
      headers["Authorization"] = `Bearer ${_accessToken}`;
    }

    const response = await fetchWithTimeout(url, {
      ...options,
      headers,
    }, timeoutMs);

    if (!response.ok) {
      let errorDetail = null;
      try {
        const errorData = await response.json();
        errorDetail = errorData.detail || errorData.error;
      } catch {
        errorDetail = response.statusText;
      }
      
      // handle non-string error details (e.g. FastAPI validation errors return arrays)
      let errorMessage: string;
      if (!errorDetail) {
        errorMessage = `HTTP ${response.status}`;
      } else if (typeof errorDetail === 'string') {
        errorMessage = errorDetail;
      } else {
        errorMessage = JSON.stringify(errorDetail);
      }

      // On 401, trigger session refresh and retry once after token is refreshed
      if (response.status === 401 && typeof window !== 'undefined') {
        const refreshTriggered = handle401();

        // Retry once after waiting for session refresh to complete
        if (refreshTriggered && !_isRetry) {
          // Wait for the session refresh to complete (up to 5s)
          await new Promise<void>((resolve) => {
            let elapsed = 0;
            const interval = setInterval(() => {
              elapsed += 200;
              if (!_sessionRefreshInFlight || elapsed >= 5000) {
                clearInterval(interval);
                resolve();
              }
            }, 200);
          });

          // Retry with potentially refreshed token
          return fetchAPI<T>(endpoint, options, timeoutMs, true);
        }

        throw new APIError(errorMessage, response.status, errorDetail);
      }

      throw new APIError(errorMessage, response.status, errorDetail);
    }

    // handle 204 No Content (no body to parse)
    if (response.status === 204) {
      return undefined as unknown as T;
    }

    return response.json();
  } catch (error) {
    // Handle network errors or timeout
    if (error instanceof APIError) {
      throw error;
    }
    // TypeError from fetch indicates network-level failure (DNS, connection refused,
    // CORS, SSL, etc.). Message varies by browser: "Failed to fetch" (Chrome),
    // "NetworkError..." (Firefox), "Load failed" (Safari).
    if (error instanceof TypeError) {
      throw new APIError('Network error - unable to connect to server', 0, 'Connection failed');
    }
    throw error;
  }
}

// VM Operations
export const vmAPI = {
  list: async (
    host?: string,
    status?: string,
    offset?: number,
    limit?: number,
  ): Promise<VMListResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (status) params.append("status", status);
    if (offset && offset > 0) params.append("offset", String(offset));
    if (limit && limit > 0) params.append("limit", String(limit));
    
    const queryString = params.toString();
    const endpoint = queryString ? `/vms?${queryString}` : "/vms";
    
    return fetchAPI<VMListResponse>(endpoint);
  },

  get: async (name: string, host: string): Promise<VMInfo> => {
    return fetchAPI<VMInfo>(`/vms/${name}?host=${encodeURIComponent(host)}`);
  },

  create: async (data: VMCreateRequest): Promise<VMCreateResponse> => {
    return fetchAPI<VMCreateResponse>("/vms", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  start: async (
    name: string,
    request: VMActionRequest
  ): Promise<VMActionResponse> => {
    return fetchAPI<VMActionResponse>(`/vms/${name}/start`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  stop: async (
    name: string,
    request: VMActionRequest
  ): Promise<VMActionResponse> => {
    return fetchAPI<VMActionResponse>(`/vms/${name}/stop`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  restart: async (
    name: string,
    request: VMActionRequest
  ): Promise<VMActionResponse> => {
    return fetchAPI<VMActionResponse>(`/vms/${name}/restart`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  delete: async (
    name: string,
    host: string,
    force = false,
    project?: string
  ): Promise<VMActionResponse> => {
    const params = new URLSearchParams({ host, force: String(force) });
    if (project) params.append("project", project);

    return fetchAPI<VMActionResponse>(
      `/vms/${encodeURIComponent(name)}?${params.toString()}`,
      { method: "DELETE" }
    );
  },

  getMetrics: async (
    host: string,
    instanceName: string,
    project = "default"
  ): Promise<InstanceMetrics> => {
    return fetchAPI<InstanceMetrics>(
      `/vms/${encodeURIComponent(host)}/${encodeURIComponent(instanceName)}/metrics?project=${encodeURIComponent(project)}`
    );
  },
};

// Image Operations
export const imageAPI = {
  listAll: async (): Promise<ImageListResponse[]> => {
    return fetchAPI<ImageListResponse[]>("/images");
  },

  listByHost: async (host: string): Promise<ImageListResponse> => {
    return fetchAPI<ImageListResponse>(`/images/${host}`);
  },

  listRemote: async (): Promise<RemoteImageListResponse> => {
    // The aggregated catalog can take ~30s on a cold cache (Ubuntu remote is
    // ~17 MB); allow a longer timeout than the 45s default.
    return fetchAPI<RemoteImageListResponse>("/images/remote", {}, 120000);
  },
};

// Node Operations
export const nodeAPI = {
  status: async (): Promise<NodesStatusResponse> => {
    return fetchAPI<NodesStatusResponse>("/nodes");
  },

  names: async (): Promise<string[]> => {
    return fetchAPI<string[]>("/nodes/names");
  },

  resources: async (nodeName: string): Promise<NodeResources> => {
    return fetchAPI<NodeResources>(`/nodes/${nodeName}/resources`);
  },

  gpus: async (nodeName: string): Promise<GpuCardListResponse> => {
    return fetchAPI<GpuCardListResponse>(
      `/nodes/${encodeURIComponent(nodeName)}/gpus`,
    );
  },

  updateDisplayName: async (
    nodeName: string,
    displayName: string
  ): Promise<{ success: boolean; message: string; node_name: string; display_name: string }> => {
    return fetchAPI(
      `/nodes/${nodeName}`,
      {
        method: "PATCH",
        body: JSON.stringify({ display_name: displayName }),
      }
    );
  },

  invalidateCache: async (nodeName: string): Promise<{ success: boolean; message: string; node_name: string }> => {
    return fetchAPI(
      `/nodes/${nodeName}/cache/invalidate`,
      {
        method: "POST",
      }
    );
  },

  invalidateAllCache: async (): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(
      `/nodes/cache/invalidate-all`,
      {
        method: "POST",
      }
    );
  },
};

// Project Operations
export const projectAPI = {
  listAll: async (): Promise<ProjectListResponse[]> => {
    return fetchAPI<ProjectListResponse[]>("/projects");
  },

  list: async (host: string): Promise<ProjectListResponse> => {
    return fetchAPI<ProjectListResponse>(`/projects/${encodeURIComponent(host)}`);
  },

  create: async (data: ProjectCreateRequest): Promise<ProjectCreateResponse> => {
    return fetchAPI<ProjectCreateResponse>("/projects", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (host: string, projectName: string, data: {
    description?: string;
    max_cpu?: number;
    max_ram?: string;
    max_storage?: string;
  }): Promise<ProjectCreateResponse> => {
    return fetchAPI<ProjectCreateResponse>(
      `/projects/${host}/${encodeURIComponent(projectName)}`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      }
    );
  },

  delete: async (host: string, projectName: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI<{ success: boolean; message: string }>(
      `/projects/${host}/${encodeURIComponent(projectName)}`,
      { method: "DELETE" }
    );
  },
};

// Tags API (Enterprise-Grade)
export const tagAPI = {
  // ============================================
  // Category Operations
  // ============================================
  
  listCategories: async (includeInactive = false): Promise<import("@/types/api").TagCategory[]> => {
    return fetchAPI<import("@/types/api").TagCategory[]>(
      `/tags/categories?include_inactive=${includeInactive}`
    );
  },

  createCategory: async (
    data: import("@/types/api").TagCategoryCreate
  ): Promise<import("@/types/api").TagCategory> => {
    return fetchAPI<import("@/types/api").TagCategory>("/tags/categories", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  updateCategory: async (
    categoryId: number,
    data: import("@/types/api").TagCategoryUpdate
  ): Promise<import("@/types/api").TagCategory> => {
    return fetchAPI<import("@/types/api").TagCategory>(
      `/tags/categories/${categoryId}`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      }
    );
  },

  // ============================================
  // Tag CRUD Operations
  // ============================================

  listTags: async (params?: {
    category_id?: number;
    search?: string;
  }): Promise<import("@/types/api").TagListResponse> => {
    const searchParams = new URLSearchParams();
    if (params?.category_id) searchParams.append("category_id", String(params.category_id));
    if (params?.search) searchParams.append("search", params.search);
    const query = searchParams.toString();
    return fetchAPI<import("@/types/api").TagListResponse>(
      `/tags${query ? `?${query}` : ""}`
    );
  },

  createTag: async (
    data: import("@/types/api").TagCreate
  ): Promise<import("@/types/api").Tag> => {
    return fetchAPI<import("@/types/api").Tag>("/tags", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  updateTag: async (
    tagId: number,
    data: import("@/types/api").TagUpdate
  ): Promise<import("@/types/api").Tag> => {
    return fetchAPI<import("@/types/api").Tag>(`/tags/${tagId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  deleteTag: async (tagId: number): Promise<void> => {
    return fetchAPI<void>(`/tags/${tagId}`, { method: "DELETE" });
  },

  // ============================================
  // Statistics
  // ============================================

  getStats: async (): Promise<import("@/types/api").TagStatsResponse> => {
    return fetchAPI<import("@/types/api").TagStatsResponse>("/tags/stats");
  },

  // ============================================
  // Resource Tag Operations (New API)
  // ============================================

  getResourceTags: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    host?: string,
    project?: string
  ): Promise<import("@/types/api").ResourceTagsResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<import("@/types/api").ResourceTagsResponse>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}${query ? `?${query}` : ""}`
    );
  },

  setResourceTags: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    tags: string[],
    host?: string,
    project?: string
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}${query ? `?${query}` : ""}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: resourceType, resource_id: resourceId }),
      }
    );
  },

  addResourceTags: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    tags: string[],
    host?: string,
    project?: string
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}/add${query ? `?${query}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ tags, resource_type: resourceType, resource_id: resourceId }),
      }
    );
  },

  removeResourceTag: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    tagName: string,
    host?: string,
    project?: string
  ): Promise<void> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<void>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}/${encodeURIComponent(tagName)}${query ? `?${query}` : ""}`,
      { method: "DELETE" }
    );
  },

  // ============================================
  // User Visibility Settings
  // ============================================

  getUserVisibility: async (): Promise<import("@/types/api").UserTagVisibilityResponse> => {
    return fetchAPI<import("@/types/api").UserTagVisibilityResponse>("/tags/visibility");
  },

  updateUserVisibility: async (
    settings: import("@/types/api").UserTagVisibilitySettings
  ): Promise<import("@/types/api").UserTagVisibilityResponse> => {
    return fetchAPI<import("@/types/api").UserTagVisibilityResponse>("/tags/visibility", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
  },

  // ============================================
  // Utility Endpoints
  // ============================================

  classifyTags: async (tags: string[]): Promise<import("@/types/api").TagGrouped> => {
    return fetchAPI<import("@/types/api").TagGrouped>("/tags/classify", {
      method: "POST",
      body: JSON.stringify(tags),
    });
  },

  validateTags: async (tags: string[]): Promise<Record<string, { valid: boolean; error: string | null }>> => {
    return fetchAPI<Record<string, { valid: boolean; error: string | null }>>("/tags/validate", {
      method: "POST",
      body: JSON.stringify(tags),
    });
  },

  initCategories: async (): Promise<{ success: boolean; message: string; created_count: number }> => {
    return fetchAPI<{ success: boolean; message: string; created_count: number }>(
      "/tags/init-categories",
      { method: "POST" }
    );
  },

  // ============================================
  // Legacy Endpoints (Backward Compatibility)
  // ============================================

  updateInstanceTags: async (
    host: string,
    instanceName: string,
    tags: string[],
    project: string = "default"
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/instances/${host}/${encodeURIComponent(instanceName)}?project=${project}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: "instance", resource_id: instanceName }),
      }
    );
  },

  updateProjectTags: async (
    host: string,
    projectName: string,
    tags: string[]
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/projects/${host}/${encodeURIComponent(projectName)}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: "project", resource_id: projectName }),
      }
    );
  },

  updateNodeTags: async (
    nodeName: string,
    tags: string[]
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/nodes/${nodeName}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: "node", resource_id: nodeName }),
      }
    );
  },

  getInstanceTags: async (
    host: string,
    instanceName: string,
    project: string = "default"
  ): Promise<import("@/types/api").ResourceTagsResponse> => {
    return fetchAPI<import("@/types/api").ResourceTagsResponse>(
      `/tags/instances/${host}/${encodeURIComponent(instanceName)}?project=${project}`
    );
  },
};

// Health Check
export const healthCheck = async (): Promise<{ status: string }> => {
  const response = await fetch("/health");
  if (!response.ok) {
    throw new APIError("Health check failed", response.status);
  }
  return response.json();
};

// System API
const systemAPI = {
  getSettings: async (): Promise<{
    read_only_mode: boolean;
    api_version: string;
    debug: boolean;
  }> => {
    return fetchAPI<{
      read_only_mode: boolean;
      api_version: string;
      debug: boolean;
    }>("/system/settings");
  },
  getVersion: async () => {
    return fetchAPI<{
      app_version: string;
      api_version: string;
      build_date?: string | null;
      build_commit?: string | null;
      environment: string;
    }>("/system/version");
  },
  getResourceUsage: async (): Promise<SystemResourceUsage> => {
    return fetchAPI<SystemResourceUsage>("/system/resource-usage");
  },
  clearCache: async (namespace?: string): Promise<{ message?: string; error?: string }> => {
    const params = new URLSearchParams();
    if (namespace) params.append("namespace", namespace);

    const queryString = params.toString();
    return fetchAPI<{ message?: string; error?: string }>(
      queryString ? `/system/cache/clear?${queryString}` : "/system/cache/clear",
      { method: "POST" }
    );
  },
};

// Instance Operations (similar to VMs but more comprehensive)
export const instanceAPI = {
  create: async (data: VMCreateRequest): Promise<VMCreateResponse> => {
    return fetchAPI<VMCreateResponse>("/instances", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  list: async (host?: string, status?: string, type?: string, offset?: number, limit?: number): Promise<VMListResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (status) params.append("status", status);
    if (type) params.append("instance_type", type);
    if (offset && offset > 0) params.append("offset", String(offset));
    if (limit && limit > 0) params.append("limit", String(limit));
    
    const queryString = params.toString();
    const endpoint = queryString ? `/instances?${queryString}` : "/instances";
    
    return fetchAPI<VMListResponse>(endpoint);
  },

  get: async (name: string, host: string): Promise<VMInfo> => {
    return fetchAPI<VMInfo>(`/instances/${name}?host=${encodeURIComponent(host)}`);
  },

  getMetrics: async (name: string, host: string): Promise<InstanceMetrics> => {
    return fetchAPI<InstanceMetrics>(`/instances/${name}/metrics?host=${encodeURIComponent(host)}`);
  },
};

// Profile Operations
export const profileAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/profiles?host=${encodeURIComponent(host)}` : "/profiles";
    return fetchAPI(endpoint);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/profiles/${name}?host=${encodeURIComponent(host)}`);
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/profiles?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (name: string, host: string, data: any) => {
    return fetchAPI(`/profiles/${name}?host=${encodeURIComponent(host)}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  delete: async (name: string, host: string) => {
    return fetchAPI(`/profiles/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Network Operations
export const networkAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/networks?host=${encodeURIComponent(host)}` : "/networks";
    return fetchAPI(endpoint);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/networks/${name}?host=${encodeURIComponent(host)}`);
  },

  getState: async (name: string, host: string) => {
    return fetchAPI(`/networks/${name}/state?host=${encodeURIComponent(host)}`);
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/networks?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (name: string, host: string, data: any) => {
    return fetchAPI(`/networks/${name}?host=${encodeURIComponent(host)}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  delete: async (name: string, host: string) => {
    return fetchAPI(`/networks/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Network ACL Operations
export const networkACLAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/network-acls?host=${encodeURIComponent(host)}` : "/network-acls";
    return fetchAPI(endpoint);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/network-acls/${name}?host=${encodeURIComponent(host)}`);
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/network-acls?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (name: string, host: string, data: any) => {
    return fetchAPI(`/network-acls/${name}?host=${encodeURIComponent(host)}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  delete: async (name: string, host: string) => {
    return fetchAPI(`/network-acls/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Storage Pool Operations
export const storagePoolAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/storage/pools?host=${encodeURIComponent(host)}` : "/storage/pools";
    return fetchAPI(endpoint);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/storage/pools/${name}?host=${encodeURIComponent(host)}`);
  },

  getResources: async (name: string, host: string) => {
    return fetchAPI(`/storage/pools/${name}/resources?host=${encodeURIComponent(host)}`);
  },

  getResourcesAll: async () => {
    return fetchAPI("/storage/pools/resources-all");
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/storage/pools?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (name: string, host: string, data: any) => {
    return fetchAPI(`/storage/pools/${name}?host=${encodeURIComponent(host)}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  delete: async (name: string, host: string) => {
    return fetchAPI(`/storage/pools/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Storage Volume Operations
export const storageVolumeAPI = {
  list: async (host?: string, pool?: string) => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (pool) params.append("pool", pool);
    
    const queryString = params.toString();
    const endpoint = queryString ? `/storage/volumes?${queryString}` : "/storage/volumes";
    
    return fetchAPI(endpoint);
  },

  get: async (volumeType: string, name: string, host: string, pool: string) => {
    return fetchAPI(
      `/storage/volumes/${volumeType}/${name}?host=${encodeURIComponent(host)}&pool=${encodeURIComponent(pool)}`
    );
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/storage/volumes?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (volumeType: string, name: string, host: string, pool: string, data: any) => {
    return fetchAPI(
      `/storage/volumes/${volumeType}/${name}?host=${encodeURIComponent(host)}&pool=${encodeURIComponent(pool)}`,
      {
        method: "PUT",
        body: JSON.stringify(data),
      }
    );
  },

  delete: async (volumeType: string, name: string, host: string, pool: string) => {
    return fetchAPI(
      `/storage/volumes/${volumeType}/${name}?host=${encodeURIComponent(host)}&pool=${encodeURIComponent(pool)}`,
      {
        method: "DELETE",
      }
    );
  },
};

// Operation Operations
export const operationAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/operations?host=${encodeURIComponent(host)}` : "/operations";
    return fetchAPI(endpoint);
  },

  get: async (id: string, host: string) => {
    return fetchAPI(`/operations/${id}?host=${encodeURIComponent(host)}`);
  },

  cancel: async (id: string, host: string) => {
    return fetchAPI(`/operations/${id}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },

  wait: async (id: string, host: string, timeout?: number) => {
    const params = new URLSearchParams({ host });
    if (timeout) params.append("timeout", timeout.toString());
    
    return fetchAPI(`/operations/${id}/wait?${params.toString()}`, {
      method: "POST",
    });
  },
};

// Warning Operations
export const warningAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/warnings?host=${encodeURIComponent(host)}` : "/warnings";
    return fetchAPI(endpoint);
  },

  get: async (uuid: string, host: string) => {
    return fetchAPI(`/warnings/${uuid}?host=${encodeURIComponent(host)}`);
  },

  delete: async (uuid: string, host: string) => {
    return fetchAPI(`/warnings/${uuid}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Server Operations
export const serverAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/server?host=${encodeURIComponent(host)}` : "/server";
    return fetchAPI(endpoint);
  },

  get: async (nodeName: string) => {
    return fetchAPI(`/server/${nodeName}`);
  },

  getResources: async (nodeName: string) => {
    return fetchAPI(`/server/${nodeName}/resources`);
  },

  updateConfig: async (nodeName: string, config: any) => {
    return fetchAPI(`/server/${nodeName}/config`, {
      method: "PUT",
      body: JSON.stringify(config),
    });
  },
};

// Identity Operations
export const identityAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/identities?host=${encodeURIComponent(host)}` : "/identities";
    return fetchAPI(endpoint);
  },

  get: async (id: string, host: string) => {
    return fetchAPI(`/identities/${id}?host=${encodeURIComponent(host)}`);
  },

  delete: async (authType: string, identityId: string, host: string) => {
    return fetchAPI(
      `/identities/${authType}/${encodeURIComponent(identityId)}?host=${encodeURIComponent(host)}`,
      { method: "DELETE" }
    );
  },
};

// Group Operations
export const groupAPI = {
  list: async (host?: string) => {
    const endpoint = host ? `/groups?host=${encodeURIComponent(host)}` : "/groups";
    return fetchAPI(endpoint);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/groups/${name}?host=${encodeURIComponent(host)}`);
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/groups?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (name: string, data: any, host: string) => {
    return fetchAPI(`/groups/${name}?host=${encodeURIComponent(host)}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  addIdentity: async (groupName: string, authType: "tls" | "oidc", identityId: string, host: string) => {
    return fetchAPI(`/groups/${groupName}/identities?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify({ auth_type: authType, identity_id: identityId }),
    });
  },

  removeIdentity: async (groupName: string, authType: string, identityId: string, host: string) => {
    return fetchAPI(
      `/groups/${groupName}/identities/${authType}/${encodeURIComponent(identityId)}?host=${encodeURIComponent(host)}`,
      {
        method: "DELETE",
      }
    );
  },

  delete: async (name: string, host: string) => {
    return fetchAPI(`/groups/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Cluster Operations
export const clusterAPI = {
  register: async (data: ClusterRegisterRequest): Promise<ClusterRegisterResponse> => {
    return fetchAPI<ClusterRegisterResponse>("/clusters/register", {
      method: "POST",
      body: JSON.stringify(data),
    }, 90000); // 90 second timeout for Vault operations and auto-detect
  },

  checkAuth: async (clusterId: string): Promise<ClusterAuthCheckResponse> => {
    return fetchAPI<ClusterAuthCheckResponse>("/clusters/check-auth", {
      method: "POST",
      body: JSON.stringify({ cluster_id: clusterId }),
    }, 20000); // 20s timeout - node should respond quickly if trust is configured
  },

  list: async (fresh: boolean = false): Promise<ClusterListResponse> => {
    const endpoint = fresh ? "/clusters?fresh=true" : "/clusters";
    return fetchAPI<ClusterListResponse>(endpoint);
  },

  get: async (clusterId: string): Promise<ClusterInfo> => {
    return fetchAPI<ClusterInfo>(`/clusters/${clusterId}`);
  },

  delete: async (clusterId: string): Promise<ClusterDeleteResponse> => {
    return fetchAPI<ClusterDeleteResponse>(`/clusters/${clusterId}`, {
      method: "DELETE",
    });
  },

  regenerateCert: async (clusterId: string): Promise<ClusterRegisterResponse> => {
    return fetchAPI<ClusterRegisterResponse>(
      `/clusters/${clusterId}/regenerate-cert`,
      {
        method: "POST",
      }
    );
  },

  updateLocation: async (clusterId: string, location: GeoLocation): Promise<ClusterLocationUpdateResponse> => {
    return fetchAPI<ClusterLocationUpdateResponse>(
      `/clusters/${clusterId}/location`,
      {
        method: "PATCH",
        body: JSON.stringify({ location }),
      }
    );
  },

  getFacilities: async (): Promise<Array<{ facility_name: string; facility_provider?: string }>> => {
    return fetchAPI<Array<{ facility_name: string; facility_provider?: string }>>("/clusters/facilities");
  },
};

// IPAM API
export const ipamAPI = {
  list: async (host?: string) => {
    const url = host ? `/ipam?host=${encodeURIComponent(host)}` : "/ipam";
    return fetchAPI(url);
  },
};

// My Keys API
export const myKeysAPI = {
  list: async (statusFilter?: string, search?: string) => {
    let url = "/my-keys";
    const params = new URLSearchParams();
    if (statusFilter) params.append("status_filter", statusFilter);
    if (search) params.append("search", search);
    if (params.toString()) url += `?${params.toString()}`;
    return fetchAPI(url);
  },

  create: async (request: any) => {
    return fetchAPI("/my-keys", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  download: async (keyId: string) => {
    const url = `${API_BASE_URL}${API_PREFIX}/my-keys/${keyId}/download`;
    const response = await fetch(url, {
      headers: {
        ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
      },
    });
    if (!response.ok) {
      throw new Error("Failed to download key");
    }
    return response.blob();
  },

  delete: async (keyId: string) => {
    return fetchAPI(`/my-keys/${keyId}`, {
      method: "DELETE",
    });
  },
};

// Monitoring API
import type { 
  MonitoringDashboardResponse, 
  ClusterMonitoringMetrics, 
  MetricsHistoryPoint,
  MonitoringNetworkMetrics,
  CertificateListResponse,
  CertificateInfo,
  CertificateRenewalRequest,
  CertificateRenewalResponse,
  CertificateVerifyRequest,
  CertificateVerifyResponse,
  SystemWarningsResponse,
} from "@/types";

export const monitoringAPI = {
  getDashboard: async (): Promise<MonitoringDashboardResponse> => {
    return fetchAPI<MonitoringDashboardResponse>("/monitoring/dashboard");
  },

  getNodeMetrics: async (nodeName: string): Promise<ClusterMonitoringMetrics> => {
    return fetchAPI<ClusterMonitoringMetrics>(`/monitoring/nodes/${encodeURIComponent(nodeName)}/metrics`);
  },

  getHistory: async (points: number = 60): Promise<MetricsHistoryPoint[]> => {
    return fetchAPI<MetricsHistoryPoint[]>(`/monitoring/history?points=${points}`);
  },

  getNetworkMetrics: async (): Promise<MonitoringNetworkMetrics> => {
    return fetchAPI<MonitoringNetworkMetrics>("/monitoring/network");
  },
};

// Certificate Management API
export const certificateAPI = {
  list: async (): Promise<CertificateListResponse> => {
    return fetchAPI<CertificateListResponse>("/certificates");
  },

  get: async (clusterId: string): Promise<CertificateInfo> => {
    return fetchAPI<CertificateInfo>(`/certificates/${encodeURIComponent(clusterId)}`);
  },

  initiateRenewal: async (request: CertificateRenewalRequest): Promise<CertificateRenewalResponse> => {
    return fetchAPI<CertificateRenewalResponse>("/certificates/renew", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  verifyRenewal: async (request: CertificateVerifyRequest): Promise<CertificateVerifyResponse> => {
    return fetchAPI<CertificateVerifyResponse>("/certificates/verify", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  cancelPending: async (clusterId: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(`/certificates/${encodeURIComponent(clusterId)}/pending`, {
      method: "DELETE",
    });
  },

  getSystemWarnings: async (): Promise<SystemWarningsResponse> => {
    return fetchAPI<SystemWarningsResponse>("/warnings/system/all");
  },

  cleanupPending: async (): Promise<{ success: boolean; message: string; cleaned_count: number }> => {
    return fetchAPI("/certificates/cleanup-pending", {
      method: "POST",
    });
  },
};

// Integrations API (External Apps - Orcahub)
import type {
  APIKeyInfo,
  APIKeyCreateRequest,
  APIKeyCreateResponse,
  APIKeyListResponse,
  ClusterPermission,
  ExternalUserMapping,
  UserMappingRequest,
  AvailableClustersResponse,
  IntegrationOrganization,
} from "@/types";

export const integrationsAPI = {
  // API Key Management
  listAPIKeys: async (): Promise<APIKeyListResponse> => {
    return fetchAPI<APIKeyListResponse>("/integrations/api-keys");
  },

  getAPIKey: async (keyId: string): Promise<APIKeyInfo> => {
    return fetchAPI<APIKeyInfo>(`/integrations/api-keys/${encodeURIComponent(keyId)}`);
  },

  createAPIKey: async (request: APIKeyCreateRequest): Promise<APIKeyCreateResponse> => {
    return fetchAPI<APIKeyCreateResponse>("/integrations/api-keys", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  updateAPIKeyPermissions: async (keyId: string, permissions: ClusterPermission[]): Promise<APIKeyInfo> => {
    return fetchAPI<APIKeyInfo>(`/integrations/api-keys/${encodeURIComponent(keyId)}/permissions`, {
      method: "PUT",
      body: JSON.stringify(permissions),
    });
  },

  revokeAPIKey: async (keyId: string): Promise<APIKeyInfo> => {
    return fetchAPI<APIKeyInfo>(`/integrations/api-keys/${encodeURIComponent(keyId)}/revoke`, {
      method: "POST",
    });
  },

  deleteAPIKey: async (keyId: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(`/integrations/api-keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
    });
  },

  // User Mappings
  listUserMappings: async (keyId: string): Promise<ExternalUserMapping[]> => {
    return fetchAPI<ExternalUserMapping[]>(`/integrations/api-keys/${encodeURIComponent(keyId)}/users`);
  },

  addUserMapping: async (keyId: string, request: UserMappingRequest): Promise<ExternalUserMapping> => {
    return fetchAPI<ExternalUserMapping>(`/integrations/api-keys/${encodeURIComponent(keyId)}/users`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  deleteUserMapping: async (keyId: string, userId: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(`/integrations/api-keys/${encodeURIComponent(keyId)}/users/${encodeURIComponent(userId)}`, {
      method: "DELETE",
    });
  },

  // Available Clusters
  getAvailableClusters: async (): Promise<AvailableClustersResponse> => {
    return fetchAPI<AvailableClustersResponse>("/integrations/available-clusters");
  },

  // Synced Organizations from external apps
  listOrganizations: async (source?: string): Promise<IntegrationOrganization[]> => {
    const params = source ? `?source=${encodeURIComponent(source)}` : "";
    return fetchAPI<IntegrationOrganization[]>(`/integrations/organizations${params}`);
  },

  refreshOrganizations: async (apiKeyId: string): Promise<{ message: string; organizations_synced: number }> => {
    return fetchAPI(`/integrations/refresh-organizations?api_key_id=${encodeURIComponent(apiKeyId)}`, {
      method: "POST",
    });
  },
};

// Regions API
import { RegionsOverviewResponse, RegionSummary } from "@/types/api";

const regionsAPI = {
  getOverview: async () => {
    return fetchAPI<RegionsOverviewResponse>("/regions");
  },
  getRegion: async (regionId: string) => {
    return fetchAPI<RegionSummary>(`/regions/${encodeURIComponent(regionId)}`);
  },
};

// Consolidated API export
export const api = {
  vms: vmAPI,
  images: imageAPI,
  nodes: nodeAPI,
  projects: projectAPI,
  tags: tagAPI,
  system: systemAPI,
  health: healthCheck,
  // New LXD Features
  instances: instanceAPI,
  profiles: profileAPI,
  networks: networkAPI,
  networkACLs: networkACLAPI,
  storagePools: storagePoolAPI,
  storageVolumes: storageVolumeAPI,
  operations: operationAPI,
  warnings: warningAPI,
  server: serverAPI,
  identities: identityAPI,
  groups: groupAPI,
  clusters: clusterAPI,
  ipam: ipamAPI,
  myKeys: myKeysAPI,
  monitoring: monitoringAPI,
  certificates: certificateAPI,
  integrations: integrationsAPI,
  regions: regionsAPI,
  clusterMembers: {
    list: async (node: string) => {
      return fetchAPI<ClusterMembersResponse>(`/cluster-members?node=${encodeURIComponent(node)}`);
    },
    get: async (node: string, memberName: string) => {
      return fetchAPI<ClusterMember>(
        `/cluster-members/${encodeURIComponent(memberName)}?node=${encodeURIComponent(node)}`
      );
    },
    getResources: async (node: string, memberName: string) => {
      return fetchAPI<MemberResources>(
        `/cluster-members/${encodeURIComponent(memberName)}/resources?node=${encodeURIComponent(node)}`
      );
    },
  },

  // ── Organizations ──
  organizations: {
    list: () => fetchAPI<Organization[]>("/organizations"),
    get: (id: string) => fetchAPI<Organization>(`/organizations/${id}`),
    create: (data: OrganizationCreateRequest) =>
      fetchAPI<Organization>("/organizations", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    update: (id: string, data: OrganizationUpdateRequest) =>
      fetchAPI<Organization>(`/organizations/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      fetchAPI<{ message: string }>(`/organizations/${id}`, { method: "DELETE" }),
    // members
    listMembers: (orgId: string) =>
      fetchAPI<OrganizationMember[]>(`/organizations/${orgId}/members`),
    addMember: (orgId: string, data: { user_email: string }) =>
      fetchAPI<OrganizationMember>(`/organizations/${orgId}/members`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    updateMember: (orgId: string, email: string, data: Record<string, unknown>) =>
      fetchAPI<OrganizationMember>(
        `/organizations/${orgId}/members/${encodeURIComponent(email)}`,
        { method: "PUT", body: JSON.stringify(data) }
      ),
    removeMember: (orgId: string, email: string) =>
      fetchAPI<{ message: string }>(
        `/organizations/${orgId}/members/${encodeURIComponent(email)}`,
        { method: "DELETE" }
      ),
    // clusters
    listClusters: (orgId: string) =>
      fetchAPI<OrganizationCluster[]>(`/organizations/${orgId}/clusters`),
    bindCluster: (orgId: string, data: { cluster_id: string }) =>
      fetchAPI<OrganizationCluster>(`/organizations/${orgId}/clusters`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    unbindCluster: (orgId: string, clusterId: string) =>
      fetchAPI<{ message: string }>(
        `/organizations/${orgId}/clusters/${encodeURIComponent(clusterId)}`,
        { method: "DELETE" }
      ),
    // org-level personalization
    getPreferences: (orgId: string) =>
      fetchAPI<{ organization_id: number; settings: Record<string, any>; updated_by?: string }>(
        `/organizations/${orgId}/preferences`
      ),
    updatePreferences: (orgId: string, settings: Record<string, any>) =>
      fetchAPI<{ organization_id: number; settings: Record<string, any>; updated_by?: string }>(
        `/organizations/${orgId}/preferences`,
        { method: "PUT", body: JSON.stringify({ settings }) }
      ),
    deletePreferences: (orgId: string) =>
      fetchAPI<{ message: string }>(`/organizations/${orgId}/preferences`, {
        method: "DELETE",
      }),
    getMyOrgPreferences: () =>
      fetchAPI<{ organization_id: number; settings: Record<string, any>; updated_by?: string; updated_at?: string }>(
        `/organizations/my-preferences`
      ),
    getMyOrgPreferencesVersion: () =>
      fetchAPI<{ organization_id: number; updated_at: string | null }>(
        `/organizations/my-preferences/version`
      ),
  },

  // ── User Groups (Admin) ──
  userGroups: {
    list: () => fetchAPI<UserGroupEntry[]>("/user-groups"),
    assign: (data: UserGroupAssignRequest) =>
      fetchAPI<UserGroupAssignResponse>("/user-groups/assign", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    remove: (email: string) =>
      fetchAPI<{ message: string }>(
        `/user-groups/${encodeURIComponent(email)}`,
        { method: "DELETE" }
      ),
    syncStatus: () => fetchAPI<AuthentikSyncStatus>("/user-groups/sync-status"),
  },

  // ── Tenant Access Policies (ACL) ──
  tenantAccess: {
    // policy CRUD
    listPolicies: (organizationId?: number) => {
      const params = organizationId ? `?organization_id=${organizationId}` : "";
      return fetchAPI<PolicyListResponse>(`/tenant-access/policies${params}`);
    },
    getPolicy: (policyId: number) =>
      fetchAPI<AccessPolicy>(`/tenant-access/policies/${policyId}`),
    createPolicy: (data: PolicyCreateRequest) =>
      fetchAPI<AccessPolicy>("/tenant-access/policies", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    updatePolicy: (policyId: number, data: PolicyUpdateRequest) =>
      fetchAPI<AccessPolicy>(`/tenant-access/policies/${policyId}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    deletePolicy: (policyId: number) =>
      fetchAPI<void>(`/tenant-access/policies/${policyId}`, { method: "DELETE" }),

    // assignments
    listAssignments: (organizationId?: number, userEmail?: string) => {
      const params = new URLSearchParams();
      if (organizationId) params.set("organization_id", String(organizationId));
      if (userEmail) params.set("user_email", userEmail);
      const qs = params.toString();
      return fetchAPI<AssignmentListResponse>(`/tenant-access/assignments${qs ? `?${qs}` : ""}`);
    },
    createAssignment: (data: AssignmentCreateRequest) =>
      fetchAPI<PolicyAssignment>("/tenant-access/assignments", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    deleteAssignment: (assignmentId: number) =>
      fetchAPI<void>(`/tenant-access/assignments/${assignmentId}`, { method: "DELETE" }),

    // tenant self-view
    myAccess: () => fetchAPI<MyAccessResponse>("/tenant-access/my-access"),
  },
};
