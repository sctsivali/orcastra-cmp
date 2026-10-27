import { fetchRead, getAccessToken, type ReadOptions } from "@/lib/api";
import type { KeyActivityPage, KeyActivityParams, KeyLastUsed } from "@/types/my-keys";

function query(params: KeyActivityParams): string {
  const search = new URLSearchParams();
  if (params.keyId) search.set("key_id", params.keyId);
  if (params.action) search.set("action", params.action);
  if (params.result) search.set("result", params.result);
  if (params.kind) search.set("kind", params.kind);
  if (params.since) search.set("since", params.since);
  if (params.until) search.set("until", params.until);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** My Keys activity: the dashboard trail of each key and what was done with it on a cluster. */
export const myKeysActivityAPI = {
  list: (params: KeyActivityParams, opts: ReadOptions = {}) =>
    fetchRead<KeyActivityPage>(`/my-keys/activity${query(params)}`, opts),

  lastUsed: (opts: ReadOptions = {}) =>
    fetchRead<{ items: KeyLastUsed[] }>("/my-keys/last-used", opts),

  /** The same rows as a CSV file, at most 10,000. */
  exportCsv: async (params: KeyActivityParams): Promise<Blob> => {
    const token = getAccessToken();
    const response = await fetch(`/api/v1/my-keys/activity/export.csv${query({ ...params, limit: undefined, offset: undefined })}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw new Error("The activity export failed. Try again.");
    return response.blob();
  },
};
