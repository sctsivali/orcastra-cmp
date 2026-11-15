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

function verdictSignature(verdict: MyAccessVerdict | undefined): string {
  return verdict ? `${verdict.has_role}:${verdict.role}:${verdict.blocked_reason}` : "none";
}

/**
 * The session's role as the backend enforces it, shared by the gate and the role-dependent shell.
 *
 * Groups that map to Admin or Partner settle it from the session alone. A Tenant by groups asks
 * the backend whether it has access. A session whose groups map to no role asks too: an account an
 * active organization lists as a member is a Tenant (a partner deployment's buyer signs in through
 * a federated IdP that sends no groups), and only the backend can see the membership.
 */
function useSessionVerdict({ enabled }: { enabled: boolean }) {
  const { data: session, status } = useSession();
  const groups = session?.user?.groups;
  // important: a session refresh (the 4 minute poll, or Check again itself) reports "loading" while
  // it keeps the session. The retained session counts, as it does for useActorQueryScope; reading
  // it as "not known" flashes the shell and unmounts the notice with its Check again result.
  const known = enabled && Boolean(session) && status !== "unauthenticated" && Array.isArray(groups);
  const groupless = known && !hasMappedRole(groups);
  const tenantByGroups = known && !groupless && getHighestRole(resolveMappedRoles(groups)) === Role.TENANT;
  const asksVerdict = groupless || tenantByGroups;

  const actorScope = getActorQueryScope(session);
  const verdictKey = [...ACCESS_GATE_KEY, actorScope] as const;
  const verdict = useQuery({
    queryKey: verdictKey,
    queryFn: ({ signal }) => accessAPI.myAccess(signal),
    enabled: asksVerdict,
    staleTime: 30_000,
    retry: 1,
    refetchOnWindowFocus: true,
    refetchInterval: (query) =>
      query.state.data?.blocked_reason ? BLOCKED_POLL_MS : ALLOWED_POLL_MS,
  });

  // The last verdict for this same actor outlives a reset of the query cache (Settings > Clear
  // Browser Cache empties it), so the gate does not fall back to "checking" and swap the page for
  // the loader in the middle of the action that cleared it.
  const lastVerdictRef = useRef<{ scope: string; value: MyAccessVerdict } | null>(null);
  if (verdict.data) lastVerdictRef.current = { scope: actorScope, value: verdict.data };
  const knownVerdict =
    verdict.data ?? (lastVerdictRef.current?.scope === actorScope ? lastVerdictRef.current.value : undefined);

  const tenantByMembership = groupless && knownVerdict?.has_role === true && knownVerdict.role === "tenant";
  return {
    session,
    groups,
    known,
    groupless,
    tenantByGroups,
    tenantByMembership,
    isTenant: tenantByGroups || tenantByMembership,
    hasRole: (known && !groupless) || tenantByMembership,
    asksVerdict,
    actorScope,
    verdictKey,
    verdict,
    knownVerdict,
  };
}

/**
 * Whether this session holds a role the backend honours: a mapped group, or a membership.
 *
 * For the parts of the shell that read role-gated routes (branding, the Vault notice), so a
 * groupless member gets them and a caller with no role does not poll routes that refuse it.
 */
export function useSessionHasRole(): boolean {
  return useSessionVerdict({ enabled: true }).hasRole;
}

/**
 * Whether the signed-in caller may see the dashboard.
 *
 * important: only a known denial blocks, so a Tenant's failed read and an unreadable session render
 * the dashboard. A session with no role group stays on the notice until the backend says it is a
 * Tenant, so a failed read keeps it there. A first verdict is "checking": rendering the shell
 * before it lands lets pages cache the empty scope of a Tenant with no access and keep showing it
 * after access is granted.
 */
export function useAccessGate({ enabled }: { enabled: boolean }) {
  const { update } = useSession();
  const queryClient = useQueryClient();
  const [rechecking, setRechecking] = useState(false);
  const {
    session, groups, groupless, tenantByGroups, isTenant, asksVerdict, verdictKey, verdict, knownVerdict,
  } = useSessionVerdict({ enabled });

  let state: AccessGateState = "allowed";
  if (groupless && !isTenant) state = !knownVerdict && verdict.isPending ? "checking" : "no_role";
  else if (tenantByGroups && knownVerdict?.has_role === false) state = "no_role";
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
  // backend, which also sees a membership granted since. A session whose groups change remounts
  // the whole tree (TokenSync keys the actor on them), so a granted role lands on a fresh dashboard.
  const checkAgain = useCallback(async (): Promise<AccessRecheck> => {
    setRechecking(true);
    try {
      const before = JSON.stringify(groups ?? null);
      const refreshed = await update({ refreshRoles: true });
      if (!refreshed) return "failed";
      if (JSON.stringify(refreshed.user?.groups ?? null) !== before) return "changed";
      if (!asksVerdict) return "unchanged";
      const previous = verdictSignature(knownVerdict);
      const next = await queryClient.fetchQuery({
        queryKey: verdictKey,
        queryFn: ({ signal }) => accessAPI.myAccess(signal),
        staleTime: 0,
      });
      return verdictSignature(next) === previous ? "unchanged" : "changed";
    } catch {
      return "failed";
    } finally {
      setRechecking(false);
    }
  }, [groups, asksVerdict, knownVerdict, queryClient, update, verdictKey]);

  return {
    state,
    email: session?.user?.email ?? null,
    rechecking,
    checkAgain,
    // The paid term behind a Tenant's own grants, for the term notice and the "access ended" date.
    tenantAccess: isTenant ? knownVerdict?.tenant_access ?? null : null,
  };
}
