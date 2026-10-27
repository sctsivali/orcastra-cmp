/** A key's activity row (GET /my-keys/activity, backend routers/my_keys_activity.py). */
export interface KeyActivityClient {
  ip: string | null;
  /** Which header the address came from; "direct" when no proxy header was trusted. */
  ip_source: string | null;
  proxy_trusted: boolean | null;
  user_agent: string | null;
}

export type KeyActivityKind = "dashboard" | "usage";

export interface KeyActivityEntry {
  id: string;
  ts: string;
  /** "dashboard": an action on the key itself. "usage": what LXD did under its certificate. */
  kind: KeyActivityKind;
  action: string | null;
  result: string | null;
  key_id: string | null;
  key_name: string | null;
  cluster_id: string | null;
  project: string | null;
  actor_email: string | null;
  message: string;
  /** Only to an administrator and to the person who acted. */
  client: KeyActivityClient | null;
}

export interface KeyActivityPage {
  entries: KeyActivityEntry[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface KeyActivityParams {
  keyId?: string;
  action?: string;
  result?: string;
  kind?: KeyActivityKind;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

export interface KeyLastUsed {
  key_id: string;
  cluster_id: string;
  last_used_at: string | null;
}
