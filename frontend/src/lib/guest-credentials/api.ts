import { fetchAPI } from "@/lib/api";
import type { GuestTarget } from "@/lib/instance-files/api";
import { APIError } from "@/types/api";

export type { GuestTarget };

export type AccountKind = "root" | "root_equivalent" | "human";
/** `empty` means a console login with no password at all. */
export type PasswordState = "set" | "none" | "locked" | "empty" | "unknown";
/** `rootpw`: sudo asks for root's password, not the user's (openSUSE). `absent`: sudo is not installed. */
export type SudoVerdict = "yes" | "no" | "rootpw" | "absent";

export interface GuestAccount {
  name: string;
  uid: number;
  kind: AccountKind;
  home: string;
  shell: string;
  password: PasswordState;
  must_change: boolean;
  scheme: string | null;
  admin_groups: string[];
  sudo: SudoVerdict | null;
}

export interface GuestCapabilities {
  create_user: boolean;
  set_password: boolean;
  expire: boolean;
  lock: boolean;
  sudo_group: string | null;
  sudo_installed: boolean;
  sudo_change: boolean;
}

export interface GuestOverview {
  guest: {
    os_id: string;
    os_name: string;
    family: string;
    selinux: boolean;
    hash_default: string | null;
    instance_type: string;
  };
  accounts: GuestAccount[];
  accounts_omitted: number;
  capabilities: GuestCapabilities;
  password_policy: { min_length: number; max_length: number };
}

export type AddressLabel = "public" | "private" | "shared" | "link_local" | "unknown";

export interface SshServerFacts {
  installed: boolean;
  running: boolean;
  socket_activated: boolean;
  units: { unit: string; state: string }[];
  /** Global values from `sshd -T`; null when they could not be read. */
  effective: Record<string, string | string[]> | null;
  notes: ("authorized_keys_not_read" | "authorized_keys_command" | "pubkey_disabled")[];
  host_keys: { type: string; bits: number | null; fingerprint: string }[];
  addresses: { address: string; interface: string; family: string; label: AddressLabel }[];
}

export interface AuthorizedKey {
  line: number;
  key_type: string | null;
  fingerprint: string | null;
  bits: number | null;
  comment: string;
  options: string[];
  weak: boolean;
}

export interface KeyList {
  user: string;
  state: "present" | "absent" | "link" | "not_regular" | "unreadable";
  entries: AuthorizedKey[];
  warnings: string[];
}

export interface KeyAddResult {
  added: boolean;
  already_present: boolean;
  entry: AuthorizedKey;
  warnings: string[];
}

export interface PasswordResult {
  user: string;
  password: string;
  must_change_applied: boolean;
  warnings: string[];
}

export interface SudoResult {
  user: string;
  sudo: SudoVerdict | null;
  admin_groups: string[];
}

export interface CreateUserStep {
  step: "create" | "read_back" | "sudo" | "password" | "expire" | "key";
  result: "ok" | "failed" | "not_effective";
  code?: string | null;
}

export interface CreateUserResult {
  account: GuestAccount | null;
  steps: CreateUserStep[];
}

export interface CreateUserBody {
  name: string;
  sudo: boolean;
  password?: string;
  must_change: boolean;
  public_key?: string;
}

function route(target: GuestTarget, name: string): string {
  const query = new URLSearchParams({
    host: target.host,
    project: target.project,
    instance_name: target.instanceName,
  });
  return `/guest-credentials/${name}?${query.toString()}`;
}

// important: above the backend's own 90 s wall clock (services/guest_credentials/gates.py) and
// below the proxy's 100 s. A slow guest is then answered by the server, whose 504 says whether a
// change was sent, rather than by a browser timeout that cannot say anything.
const TIMEOUT_MS = 95_000;

// note: every route is POST, reads included, so a Partner cluster past its paid term refuses them
// as it refuses the console. Secrets travel in the body only, never in the query string.
export const guestCredentialsAPI = {
  overview: (target: GuestTarget, signal?: AbortSignal) =>
    fetchAPI<GuestOverview>(route(target, "overview"), { method: "POST", signal }, TIMEOUT_MS),
  sshServer: (target: GuestTarget, signal?: AbortSignal) =>
    fetchAPI<SshServerFacts>(route(target, "ssh-server"), { method: "POST", signal }, TIMEOUT_MS),
  listKeys: (target: GuestTarget, user: string, signal?: AbortSignal) =>
    fetchAPI<KeyList>(route(target, "keys/list"), { method: "POST", body: JSON.stringify({ user }), signal }, TIMEOUT_MS),
  addKey: (target: GuestTarget, user: string, publicKey: string) =>
    fetchAPI<KeyAddResult>(
      route(target, "keys/add"),
      { method: "POST", body: JSON.stringify({ user, public_key: publicKey }) },
      TIMEOUT_MS,
    ),
  removeKey: (target: GuestTarget, user: string, fingerprint: string) =>
    fetchAPI<{ removed: number }>(
      route(target, "keys/remove"),
      { method: "POST", body: JSON.stringify({ user, fingerprint }) },
      TIMEOUT_MS,
    ),
  setPassword: (target: GuestTarget, user: string, password: string, mustChange: boolean) =>
    fetchAPI<PasswordResult>(
      route(target, "password/set"),
      { method: "POST", body: JSON.stringify({ user, password, must_change: mustChange }) },
      TIMEOUT_MS,
    ),
  lockPassword: (target: GuestTarget, user: string) =>
    fetchAPI<PasswordResult>(route(target, "password/lock"), { method: "POST", body: JSON.stringify({ user }) }, TIMEOUT_MS),
  setSudo: (target: GuestTarget, user: string, enabled: boolean) =>
    fetchAPI<SudoResult>(route(target, "users/sudo"), { method: "POST", body: JSON.stringify({ user, enabled }) }, TIMEOUT_MS),
  createUser: (target: GuestTarget, body: CreateUserBody) =>
    fetchAPI<CreateUserResult>(route(target, "users/create"), { method: "POST", body: JSON.stringify(body) }, TIMEOUT_MS),
};

interface RefusalDetail {
  error?: unknown;
  message?: unknown;
  reason?: unknown;
  tool?: unknown;
}

function detailOf(error: unknown): RefusalDetail | undefined {
  if (!(error instanceof APIError)) return undefined;
  const detail = error.detail;
  return detail && typeof detail === "object" ? (detail as RefusalDetail) : undefined;
}

/** The stable code on a refusal, for choosing what to show. */
export function credentialErrorCode(error: unknown): string | undefined {
  const code = detailOf(error)?.error;
  return typeof code === "string" ? code : undefined;
}

/** The parser's reason for an `invalid_public_key` refusal. */
export function keyRejectionReason(error: unknown): string | undefined {
  const reason = detailOf(error)?.reason;
  return typeof reason === "string" ? reason : undefined;
}

/** The tool a `tool_missing` refusal names; always one of the backend's own names. */
export function missingTool(error: unknown): string | undefined {
  const tool = detailOf(error)?.tool;
  return typeof tool === "string" ? tool : undefined;
}

/**
 * The change was sent and no answer confirmed it: the server said so, or the request ran out of
 * time or lost its connection on the way back. Never reported as a failure, because a person who
 * reads "failed" assumes the old password still works.
 */
export function isOutcomeUnknown(error: unknown): boolean {
  if (!(error instanceof APIError)) return false;
  const code = credentialErrorCode(error);
  if (code === "credential_outcome_unknown") return true;
  return code === undefined && (error.status === 0 || error.status === 504 || error.status === 502);
}

/** Whether the guest may now differ from what the panel shows, so its reads are worth repeating. */
export function changeMayHaveLanded(error: unknown): boolean {
  if (!(error instanceof APIError)) return true;
  // Refused before anything reached the guest: validation, the budget, or the caller's access.
  return ![401, 403, 422, 429].includes(error.status);
}

/** Seconds to wait before trying again, for a 429. */
export function retryAfterSeconds(error: unknown): number | undefined {
  return error instanceof APIError && error.status === 429 ? error.retryAfter : undefined;
}
