import type { GuestTarget } from "./api";

export type CredentialsView = "overview" | "ssh" | "keys";

// Carries the actor, as lib/instance-files/keys.ts does: what a guest's accounts look like is what
// one person may see, and a query cache outlives sign-in. Never a password or a pasted key.
export function credentialsScope(scopeKey: string, target: GuestTarget) {
  return ["guestCredentials", scopeKey, target.host, target.project, target.instanceName] as const;
}

export function credentialsKey(scopeKey: string, target: GuestTarget, view: CredentialsView, user?: string) {
  const base = [...credentialsScope(scopeKey, target), view] as const;
  return user === undefined ? base : ([...base, user] as const);
}
