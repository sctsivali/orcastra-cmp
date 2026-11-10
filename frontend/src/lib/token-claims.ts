/**
 * The `groups` claim of an access token the IdP just issued, or null when it cannot be read.
 *
 * The payload is decoded without verification: the token comes straight from the token endpoint in
 * the same server-side request, and the backend verifies every token it is shown. This keeps the
 * session's copy of the groups in step with the claim the backend decides on, so a role change
 * reaches the page at the next refresh instead of the next sign-in. Edge-safe: atob and
 * TextDecoder only.
 */
export function readGroupsClaim(jwt: string | null | undefined): string[] | null {
  const payload = jwt?.split(".")[1];
  if (!payload) return null;
  try {
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    const claims: unknown = JSON.parse(new TextDecoder().decode(bytes));
    const groups = (claims as { groups?: unknown } | null)?.groups;
    if (!Array.isArray(groups) || !groups.every((group) => typeof group === "string")) return null;
    return groups;
  } catch {
    return null;
  }
}

/** The role_* subset the session carries beside the groups. */
export function roleGroups(groups: string[]): string[] {
  return groups.filter((group) => group.startsWith("role_"));
}
