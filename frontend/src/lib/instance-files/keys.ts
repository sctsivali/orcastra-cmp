import type { GuestTarget } from "./api";

// Carries the actor: a listing is what one person may see, and a query cache outlives sign-in.
export function directoryKey(scopeKey: string, target: GuestTarget, path: string) {
  return ["instanceFiles", scopeKey, target.host, target.project, target.instanceName, path] as const;
}

export function fileContentKey(scopeKey: string, target: GuestTarget, path: string) {
  return ["instanceFileContent", scopeKey, target.host, target.project, target.instanceName, path] as const;
}
