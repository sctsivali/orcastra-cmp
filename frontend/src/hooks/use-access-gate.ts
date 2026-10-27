"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { accessAPI, type MyAccessVerdict } from "@/lib/access-api";
import { getActorQueryScope } from "@/hooks/use-query-scope";
import { getHighestRole, hasMappedRole, resolveMappedRoles, Role } from "@/types/rbac";

export type AccessGateState = "allowed" | "checking" | "no_role" | "no_tenant_access";
export type AccessRecheck = "changed" | "unchanged" | "failed";

const ACCESS_GATE_KEY = ["access-gate"] as const;
const BLOCKED_POLL_MS = 60_000;
const ALLOWED_POLL_MS = 5 * 60_000;

/**
 * Whether the signed-in caller may see the dashboard (issue #17).
 *
 * A caller whose groups map to no role is known from the session alone. A Tenant asks the backend,
 * because "no cluster access yet" is decided by the same scope the API enforces. Admin and
 * Partner never ask and are never blocked.
 *
 * important: only a known denial blocks, so a failed read and an unreadable session render the
 * dashboard. A Tenant's first verdict is "checking" rather than "allowed": rendering the shell
 * while it loaded let Home, Instances and Projects read the empty scope of a Tenant with no access,
 * and those zeros were still cached, and shown, after access was granted.
 */
export function useAccessGate({ enabled }: { enabled: boolean }) {
  const { data: session, status, update } = useSession();
  const queryClient = useQueryClient();
  const [rechecking, setRechecking] = useState(false);
  const groups = session?.user?.groups;
  // important: a session refresh (the 4 minute poll, or Check again itself) reports "loading" while
  // it keeps the session. Reading that as "not known" let the shell render for a moment and
  // unmounted the notice with its Check again result, so the retained session counts, as it does
  // for useActorQueryScope.
  const known = enabled && Boolean(session) && status !== "unauthenticated" && Array.isArray(groups);
  const noRole = known && !hasMappedRole(groups);
  const isTenant = known && !noRole && getHighestRole(resolveMappedRoles(groups)) === Role.TENANT;

  const actorScope = getActorQueryScope(session);
  const verdictKey = [...ACCESS_GATE_KEY, actorScope] as const;
  const verdict = useQuery({
    queryKey: verdictKey,
    queryFn: ({ signal }) => accessAPI.myAccess(signal),
    enabled: isTenant,
    staleTime: 30_000,
    retry: 1,
    refetchOnWindowFocus: true,
    refetchInterval: (query) =>
      query.state.data?.blocked_reason ? BLOCKED_POLL_MS : ALLOWED_POLL_MS,
  });

  // note: the last verdict for this same actor outlives a reset of the query cache (Settings >
  // Clear Browser Cache empties it). Without it the gate fell back to "checking" and swapped the
  // page for the loader in the middle of the action that cleared it.
  const lastVerdictRef = useRef<{ scope: string; value: MyAccessVerdict } | null>(null);
  if (verdict.data) lastVerdictRef.current = { scope: actorScope, value: verdict.data };
  const knownVerdict =
    verdict.data ?? (lastVerdictRef.current?.scope === actorScope ? lastVerdictRef.current.value : undefined);

  let state: AccessGateState = "allowed";
  if (noRole || (isTenant && knownVerdict?.has_role === false)) state = "no_role";
  else if (isTenant && knownVerdict?.blocked_reason === "no_tenant_access") state = "no_tenant_access";
  else if (isTenant && !knownVerdict && verdict.isPending) state = "checking";

  // On the way in after a denial, drop whatever was read before: it was read under the scope
  // that was denied, or before a revoke, and is not this caller's picture any more.
  const wasBlockedRef = useRef(false);
  useEffect(() => {
    const blocked = state === "no_role" || state === "no_tenant_access";
    if (wasBlockedRef.current && state === "allowed") {
      queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== ACCESS_GATE_KEY[0] });
    }
    if (state !== "checking") wasBlockedRef.current = blocked;
  }, [state, queryClient]);

  // "Check again": ask the IdP for the groups now rather than at the next refresh, then ask the
  // backend. A session whose groups change remounts the whole tree (TokenSync keys the actor on
  // them), so a granted role lands on a fresh dashboard rather than on this notice.
  const checkAgain = useCallback(async (): Promise<AccessRecheck> => {
    setRechecking(true);
    try {
      const before = JSON.stringify(groups ?? null);
      const refreshed = await update({ refreshRoles: true });
      if (!refreshed) return "failed";
      if (JSON.stringify(refreshed.user?.groups ?? null) !== before) return "changed";
      if (!isTenant) return "unchanged";
      const previous = verdict.data?.blocked_reason ?? null;
      const next = await queryClient.fetchQuery({
        queryKey: verdictKey,
        queryFn: ({ signal }) => accessAPI.myAccess(signal),
        staleTime: 0,
      });
      return next.blocked_reason === previous ? "unchanged" : "changed";
    } catch {
      return "failed";
    } finally {
      setRechecking(false);
    }
  }, [groups, isTenant, queryClient, update, verdict.data?.blocked_reason, verdictKey]);

  return {
    state,
    email: session?.user?.email ?? null,
    rechecking,
    checkAgain,
    // The paid term behind a Tenant's own grants, for the term notice and the "access ended" date.
    tenantAccess: isTenant ? knownVerdict?.tenant_access ?? null : null,
  };
}
