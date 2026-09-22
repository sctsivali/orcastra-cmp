import { API_URL, getAccessToken } from "@/lib/api";

export interface FilePushTarget {
  host: string;
  instanceName: string;
  project: string;
  destinationPath: string;
  mode?: string;
  uid?: number;
  gid?: number;
}

/** Multipart guest upload. Kept outside fetchAPI because JSON buffering would corrupt the file. */
export const fileTransferAPI = {
  push: async (file: File, target: FilePushTarget): Promise<void> => {
    const formData = new FormData();
    formData.append("file", file);
    const params = new URLSearchParams({
      host: target.host,
      instance_name: target.instanceName,
      project: target.project,
      dest_path: target.destinationPath,
      mode: target.mode ?? "0644",
      uid: String(target.uid ?? 0),
      gid: String(target.gid ?? 0),
    });
    const token = getAccessToken();
    const response = await fetch(`${API_URL}/file-transfer/push?${params.toString()}`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: "Upload failed" }));
      throw new Error(error.detail || "Upload failed");
    }
  },
};
