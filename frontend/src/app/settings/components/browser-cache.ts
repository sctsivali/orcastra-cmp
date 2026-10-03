import type { Query, QueryClient } from "@tanstack/react-query";

/** Preferences and identity are persistent choices, not inventory read caches. */
export function isApplicationReadQuery(query: Pick<Query, "queryKey">): boolean {
  const family = typeof query.queryKey[0] === "string" ? query.queryKey[0].toLowerCase() : "";
  const identity = ["auth", "session", "auth-session", "next-auth", "authentication"];
  return Boolean(family) && !identity.includes(family) && !/preference|personalization/.test(family);
}

export async function clearBrowserReadCache(
  queryClient: QueryClient,
  isCurrent: () => boolean,
): Promise<boolean> {
  if (!isCurrent()) return false;
  await queryClient.cancelQueries({ predicate: isApplicationReadQuery });
  if (!isCurrent()) return false;
  // important: QueryClient.clear() also destroys the mutation cache. Keep accepted writes alive.
  queryClient.removeQueries({ predicate: isApplicationReadQuery, type: "inactive" });
  await queryClient.resetQueries(
    { predicate: isApplicationReadQuery, type: "active" },
    { throwOnError: true },
  );
  return isCurrent();
}
