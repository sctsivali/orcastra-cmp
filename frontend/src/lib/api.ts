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
  NodesStatusResponse,
  NodeResources,
  ProjectListResponse,
  ProjectCreateRequest,
  ProjectCreateResponse,
  InstanceMetrics,
  APIError,
} from "@/types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:4050";
const API_PREFIX = "/api/v1";

// Export for direct use in components
export const API_URL = `${API_BASE_URL}${API_PREFIX}`;

// Timeout wrapper for fetch with AbortController
async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeoutMs: number = 30000
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

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
      throw new APIError('Request timeout - server took too long to respond', 504, 'Request timeout');
    }
    throw error;
  }
}

async function fetchAPI<T>(
  endpoint: string,
  options: RequestInit = {},
  timeoutMs: number = 30000
): Promise<T> {
  const url = `${API_BASE_URL}${API_PREFIX}${endpoint}`;

  try {
    const response = await fetchWithTimeout(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
      },
    }, timeoutMs);

    if (!response.ok) {
      let errorDetail = null;
      try {
        const errorData = await response.json();
        errorDetail = errorData.detail || errorData.error;
      } catch {
        errorDetail = response.statusText;
      }
      
      // Create more informative error message
      const errorMessage = errorDetail || `HTTP ${response.status}`;
      throw new Error(errorMessage);
    }

    return response.json();
  } catch (error) {
    // Handle network errors or timeout
    if (error instanceof APIError) {
      throw error;
    }
    if (error instanceof TypeError && error.message.includes('fetch')) {
      throw new APIError('Network error - unable to connect to server', 0, 'Connection failed');
    }
    throw error;
  }
}

// VM Operations
export const vmAPI = {
  list: async (
    host?: string,
    status?: string
  ): Promise<VMListResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (status) params.append("status", status);
    
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
    force = false
  ): Promise<VMActionResponse> => {
    return fetchAPI<VMActionResponse>(
      `/vms/${name}?host=${encodeURIComponent(host)}&force=${force}`,
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
};

// Project Operations
export const projectAPI = {
  listAll: async (): Promise<ProjectListResponse[]> => {
    return fetchAPI<ProjectListResponse[]>("/projects");
  },

  list: async (host: string): Promise<ProjectListResponse> => {
    return fetchAPI<ProjectListResponse>(`/projects/${host}`);
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

// Tags API
export const tagAPI = {
  updateInstanceTags: async (
    host: string,
    instanceName: string,
    tags: string[],
    project: string = "default"
  ): Promise<{ success: boolean; message: string; tags: string[] }> => {
    return fetchAPI<{ success: boolean; message: string; tags: string[] }>(
      `/tags/instances/${host}/${encodeURIComponent(instanceName)}?project=${project}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags }),
      }
    );
  },

  updateProjectTags: async (
    host: string,
    projectName: string,
    tags: string[]
  ): Promise<{ success: boolean; message: string; tags: string[] }> => {
    return fetchAPI<{ success: boolean; message: string; tags: string[] }>(
      `/tags/projects/${host}/${encodeURIComponent(projectName)}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags }),
      }
    );
  },

  updateNodeTags: async (
    nodeName: string,
    tags: string[]
  ): Promise<{ success: boolean; message: string; tags: string[] }> => {
    return fetchAPI<{ success: boolean; message: string; tags: string[] }>(
      `/tags/nodes/${nodeName}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags }),
      }
    );
  },
};

// Health Check
export const healthCheck = async (): Promise<{ status: string }> => {
  const response = await fetch(`${API_BASE_URL}/health`);
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
};

// Instance Operations (similar to VMs but more comprehensive)
export const instanceAPI = {
  list: async (host?: string, status?: string, type?: string) => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (status) params.append("status", status);
    if (type) params.append("instance_type", type);
    
    const queryString = params.toString();
    const endpoint = queryString ? `/instances?${queryString}` : "/instances";
    
    return fetchAPI(endpoint);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/instances/${name}?host=${encodeURIComponent(host)}`);
  },

  getMetrics: async (name: string, host: string) => {
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

  delete: async (name: string, host: string) => {
    return fetchAPI(`/groups/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
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
};
