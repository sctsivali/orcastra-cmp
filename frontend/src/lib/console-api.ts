import { fetchAPI } from "@/lib/api";

const segment = (value: string): string => encodeURIComponent(value);

function scopedQuery(host: string, project?: string): string {
  const query = new URLSearchParams({ host });
  if (project) query.set("project", project);
  return query.toString();
}

export interface ConsoleActionResponse {
  success: boolean;
  message?: string | null;
  error?: string | null;
}

export interface ConsoleISOInfo {
  name: string;
  pool: string;
  volume: string;
  path: string;
  size: number;
  created_at?: string | null;
}

export interface ConsoleISOListResponse {
  success: boolean;
  isos: ConsoleISOInfo[];
  error?: string | null;
}

export interface AttachedISOResponse {
  success: boolean;
  attached: boolean;
  device_name?: string | null;
  source?: string | null;
  pool?: string | null;
  boot_priority: number;
  error?: string | null;
}

export interface AttachISORequest {
  pool: string;
  volume: string;
  device_name: string;
  boot_priority: number;
}

export interface StartupOrderRow {
  instance_name: string;
  host: string;
  project: string;
  priority: number;
  delay: number;
  autostart: boolean;
  instance_type: string;
  status: string;
}

export interface StartupOrderResponse {
  success: boolean;
  entries: StartupOrderRow[];
  error?: string | null;
}

export interface StartupOrderUpdate {
  instance_name: string;
  priority: number;
  delay: number;
  autostart: boolean;
}

function requireSuccess<T extends { success: boolean; error?: string | null; message?: string | null }>(
  result: T,
  fallback: string,
): T {
  if (!result.success) throw new Error(result.error || result.message || fallback);
  return result;
}

/** Typed console HTTP surface. WebSocket session creation remains owned by the console clients. */
export const consoleAPI = {
  getAttachedISO: async (
    instanceName: string,
    host: string,
    project: string,
  ): Promise<AttachedISOResponse> => {
    const result = await fetchAPI<AttachedISOResponse>(
      `/console/${segment(instanceName)}/iso?${scopedQuery(host, project)}`,
    );
    return requireSuccess(result, "Failed to read the attached ISO");
  },

  listISOs: async (host: string): Promise<ConsoleISOListResponse> => {
    const result = await fetchAPI<ConsoleISOListResponse>(
      `/console/isos?${scopedQuery(host)}`,
    );
    return requireSuccess(result, "Failed to read available ISOs");
  },

  attachISO: async (
    instanceName: string,
    host: string,
    project: string,
    request: AttachISORequest,
  ): Promise<ConsoleActionResponse> => {
    const result = await fetchAPI<ConsoleActionResponse>(
      `/console/${segment(instanceName)}/iso/attach?${scopedQuery(host, project)}`,
      { method: "POST", body: JSON.stringify(request) },
    );
    return requireSuccess(result, "Failed to attach the ISO");
  },

  ejectISO: async (
    instanceName: string,
    host: string,
    project: string,
    deviceName: string,
  ): Promise<ConsoleActionResponse> => {
    const query = new URLSearchParams({ host, project, device_name: deviceName });
    const result = await fetchAPI<ConsoleActionResponse>(
      `/console/${segment(instanceName)}/iso/eject?${query.toString()}`,
      { method: "POST" },
    );
    return requireSuccess(result, "Failed to eject the ISO");
  },

  getStartupOrder: async (host: string, project: string): Promise<StartupOrderResponse> => {
    const result = await fetchAPI<StartupOrderResponse>(
      `/console/startup-order?${scopedQuery(host, project)}`,
    );
    return requireSuccess(result, "Failed to read the startup order");
  },

  updateStartupOrder: async (
    host: string,
    project: string,
    updates: StartupOrderUpdate[],
  ): Promise<ConsoleActionResponse> => {
    const result = await fetchAPI<ConsoleActionResponse>(
      `/console/startup-order?${scopedQuery(host, project)}`,
      { method: "PUT", body: JSON.stringify({ updates }) },
    );
    return requireSuccess(result, "Failed to update the startup order");
  },
};
