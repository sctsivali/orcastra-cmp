import { isCancelledError, type Query, type QueryClient } from "@tanstack/react-query";

/** Every read query except identity and preferences, which are persistent choices. */
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
  // important: not QueryClient.clear(), which also destroys the mutation cache and accepted writes.
  queryClient.removeQueries({ predicate: isApplicationReadQuery, type: "inactive" });
  try {
    await queryClient.resetQueries(
      { predicate: isApplicationReadQuery, type: "active" },
      { throwOnError: true },
    );
  } catch (error) {
    // important: a view that depends on a reset read can unmount while its own reads refetch.
    // The task panel renders only on known /system/settings, so the reset unmounts it and React
    // Query cancels its summary read, rejecting with CancelledError although the cache was
    // cleared. Wait for the reads still in flight instead, so only a real failure rejects.
    if (!isCancelledError(error)) throw error;
    await queryClient.refetchQueries(
      { predicate: isApplicationReadQuery, type: "active" },
      { throwOnError: true, cancelRefetch: false },
    );
  }
  return isCurrent();
}
