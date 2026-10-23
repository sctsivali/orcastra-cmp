/**
 * API Client for the Orcastra CMP Backend
 */

import {
  IPAMListResponse,
  KeyCreateRequest,
  KeyInfo,
  NetworkACLDetail,
  ProfileCreateRequest,
  ProfileDetail,
  ProfileUpdateRequest,
  ProfileWriteResponse,
  VMListResponse,
  VMInfo,
  VMCreateRequest,
  InstanceCreateRequest,
  InstanceCreatePreflightResponse,
  InstanceWritePolicy,
  InstanceOperationJob,
  InstanceSpecificationDetail,
  InstanceSpecificationPreflightRequest,
  InstanceSpecificationPreflightResponse,
  InstanceSpecificationUpdateRequest,
  VMCreateResponse,
  VMActionRequest,
  VMActionResponse,
  ImageListResponse,
  ImageUsageResponse,
  RemoteImageListResponse,
  NodesStatusResponse,
  ClusterAvailabilityResponse,
  NodeResources,
  GpuCardListResponse,
  ProjectListResponse,
  ProjectUsageResponse,
  ProjectCreateRequest,
  ProjectCreateResponse,
  InstanceMetrics,
  InstanceSpecificationsResponse,
  APIError,
  ClusterRegisterRequest,
  ClusterRegisterResponse,
  ClusterAuthCheckResponse,
  ClusterListResponse,
  ClusterInfo,
  ClusterDeleteResponse,
  ClusterMembersResponse,
  ClusterMember,
  MemberResources,
  VersionInfo,
  SystemSettings,
  SystemResourceUsage,
  GeoLocation,
  ClusterLocationUpdateResponse,
  Organization,
  DesktopControlPolicy,
  DesktopControlPolicyRequest,
  OrganizationCreateRequest,
  OrganizationUpdateRequest,
  OrganizationMember,
  OrganizationCluster,
  UserGroupEntry,
  UserGroupAssignRequest,
  UserGroupAssignResponse,
  AuthentikSyncStatus,
  PolicyListResponse,
  AccessPolicy,
  PolicyCreateRequest,
  PolicyUpdateRequest,
  AssignmentListResponse,
  PolicyAssignment,
  AssignmentCreateRequest,
  MyAccessResponse,
  SnapshotListResponse,
  FleetSnapshotListResponse,
  SnapshotScheduleListResponse,
  SnapshotScheduleActionResponse,
  SnapshotScheduleCreateRequest,
  SnapshotScheduleUpdateRequest,
  SnapshotCreateRequest,
  SnapshotCreateResponse,
  SnapshotRestoreRequest,
  SnapshotActionResponse,
} from "@/types";

const API_PREFIX = "/api/v1";

// All HTTP API calls use relative URLs, and Next.js rewrites proxy them to the backend.
// This avoids cross-origin (CORS) and mixed-content issues entirely.
const API_BASE_URL = "";

// Relative API URL, safe for browser HTTP fetch calls.
// WebSocket connections use ws-url.ts which derives URLs from NEXT_PUBLIC_API_URL.
export const API_URL = API_PREFIX;

/**
 * Token store for Bearer auth.
 * Components call setAccessToken() after session load;
 * fetchAPI() automatically attaches it to all backend requests.
 */
let _accessToken: string | null = null;
let _authActorScope: string | null = null;
let _authGeneration = 0;
let _authActorGeneration = 0;
let _onTokenRefreshed: (() => void) | null = null;
const _authActorChangedListeners = new Set<() => void>();

/**
 * Register a callback fired when the access token is REPLACED with a new value
 * (silent refresh / rotation), so cached query errors from the old token can be
 * refetched. Registered by a component inside QueryClientProvider.
 */
export function onTokenRefreshed(fn: () => void) {
  _onTokenRefreshed = fn;
}

/**
 * Subscribe to authenticated actor boundaries.
 *
 * A listener runs synchronously after the new context is installed and before TokenSync marks
 * it ready. Throwing therefore fails closed: descendants stay withheld instead of mounting B
 * above A's browser cache.
 */
export function onAuthActorChanged(fn: () => void): () => void {
  _authActorChangedListeners.add(fn);
  return () => {
    _authActorChangedListeners.delete(fn);
  };
}

export function setAccessToken(
  token: string | null | undefined,
  actorScope?: string | null,
) {
  const next = token ?? null;
  const nextActorScope = next === null ? null : actorScope ?? _authActorScope;
  const tokenChanged = next !== _accessToken;
  const actorChanged = nextActorScope !== _authActorScope;
  // A silent rotation belongs to the same actor. A direct A -> B sign-in is an identity
  // boundary, not a reason to refetch A's failed queries under B's credentials.
  const rotated =
    tokenChanged &&
    !actorChanged &&
    next !== null &&
    _accessToken !== null;

  if (tokenChanged || actorChanged) {
    // A monotonically increasing, non-secret request scope. Never put a bearer token in a map
    // key: keys are inspectable in a debugger and are too easy to include in diagnostics.
    _authGeneration += 1;
  }
  if (actorChanged) {
    _authActorGeneration += 1;
    // Auth recovery state belongs to the actor that produced the 401. A completed refresh or
    // in-place prompt from A must not suppress B's own recovery attempt.
    _refreshCompleted = false;
    _authFailureAnnounced = false;
    _sessionRefreshInFlight = false;
  }

  _accessToken = next;
  _authActorScope = nextActorScope;
  if (actorChanged) {
    for (const listener of _authActorChangedListeners) listener();
  }
  if (rotated && _onTokenRefreshed) {
    _onTokenRefreshed();
  }
}

export function getAccessToken(): string | null {
  return _accessToken;
}

/** True while the installed bearer still belongs to this actor, including during rotation. */
export function isAuthActorSynchronized(actorScope: string): boolean {
  return _accessToken !== null && _authActorScope === actorScope;
}

/** Monotonic identity-boundary token. Same-actor access-token rotation does not advance it. */
export function getAuthActorGeneration(): number {
  return _authActorGeneration;
}

/** True only after TokenSync has installed the session's complete auth context. */
export function isAuthContextSynchronized(
  token: string | null | undefined,
  actorScope: string | null,
): boolean {
  const expectedToken = token ?? null;
  const expectedActorScope = expectedToken === null ? null : actorScope;
  return _accessToken === expectedToken && _authActorScope === expectedActorScope;
}

/**
 * 401 recovery, as two states rather than a count.
 *
 * important: never navigate. A hard redirect here throws the operator off the page
 * they were on and loses all in-memory state, open consoles included via the
 * beforeunload kill-all-sessions beacon. Auth death is announced to the UI, which
 * renders a re-auth prompt in place, so the current route survives.
 *
 * important: no counter and no time window. Counting attempts inside a sliding
 * window cannot work here: real polls are 10 to 60 seconds apart, so any window
 * short enough to bound a burst is also short enough to reset between two genuine
 * failures, and the prompt becomes unreachable. The question is not "how many
 * 401s" but "did refreshing help", which is one bit: once a refresh has completed
 * and a request still comes back 401, refreshing is not the answer and the
 * operator has to be told. Parallel bursts are collapsed by the in-flight flag,
 * not by arithmetic.
 */
let _refreshCompleted = false; // a session refresh finished since the last success
let _authFailureAnnounced = false;
let _sessionRefreshFn: (() => Promise<void>) | null = null;
let _sessionRefreshInFlight = false;
let _onAuthFailure: (() => void) | null = null;
let _onAuthRecovered: (() => void) | null = null;

/**
 * Register a callback for requesting a session refresh (called by auth-provider).
 * When the API layer sees a 401, it calls this to trigger useSession().update()
 * instead of redirecting.
 */
export function onSessionRefreshNeeded(fn: () => Promise<void>) {
  _sessionRefreshFn = fn;
}

/** Fired when refreshing the session has failed to clear 401s. */
export function onAuthFailure(fn: () => void) {
  _onAuthFailure = fn;
}

/** Fired when requests start working again, so the UI can drop the prompt. */
export function onAuthRecovered(fn: () => void) {
  _onAuthRecovered = fn;
}

function handle401() {
  if (_authFailureAnnounced) return false;

  // a refresh is already running: this request is part of the same burst
  if (_sessionRefreshInFlight) return false;

  if (!_refreshCompleted && _sessionRefreshFn) {
    const refreshActorGeneration = _authActorGeneration;
    _sessionRefreshInFlight = true;
    _sessionRefreshFn().finally(() => {
      // A direct account transition makes this A refresh irrelevant to B. setAccessToken()
      // already released B's in-flight gate, so the old promise must not close it again.
      if (_authActorGeneration !== refreshActorGeneration) return;
      _sessionRefreshInFlight = false;
      // whatever the outcome, we have now tried. A further 401 means it did not help.
      _refreshCompleted = true;
    });
    return true; // signal: refresh triggered, caller may retry
  }

  // a refresh has completed and requests are still refused
  _authFailureAnnounced = true;
  console.warn("[Auth] a session refresh did not clear 401s, prompting re-auth in place");
  _onAuthFailure?.();
  return false;
}

/**
 * Clear the auth-failure state. The only correct trigger is a request actually
 * succeeding: not a timer, and not a React effect re-running. auth-provider's
 * effect re-fires whenever useSession() rebuilds its memo, which update() does on
 * every refresh, so resetting from there erased the very state being tracked.
 *
 * note: no longer a counter. The threshold this used to hold was removed when the
 * handler became two states, and only the stale name survived.
 */
function clearAuthFailureState() {
  const wasAnnounced = _authFailureAnnounced;
  _refreshCompleted = false;
  _authFailureAnnounced = false;
  if (wasAnnounced) _onAuthRecovered?.();
}

// note: just above GRANT_CAP_MS in auth.ts, the longest a session read waits on the IdP. The
// loop ends as soon as the refresh settles, so only a slow IdP waits this long.
const SESSION_REFRESH_WAIT_MS = 21_000;

function waitForSessionRefresh(signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    let elapsed = 0;
    let interval: ReturnType<typeof setInterval> | undefined;

    const cleanup = () => {
      if (interval) clearInterval(interval);
      signal?.removeEventListener("abort", onAbort);
    };
    const finish = () => {
      cleanup();
      resolve();
    };
    const onAbort = () => {
      cleanup();
      reject(signal?.reason ?? new DOMException("The request was cancelled", "AbortError"));
    };

    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener("abort", onAbort, { once: true });
    interval = setInterval(() => {
      elapsed += 200;
      if (!_sessionRefreshInFlight || elapsed >= SESSION_REFRESH_WAIT_MS) finish();
    }, 200);
  });
}

// Timeout wrapper for fetch with AbortController
/** Retry-After as whole seconds: an integer delay or an HTTP-date, undefined when absent or bad. */
function parseRetryAfter(header: string | null): number | undefined {
  if (!header) return undefined;
  const value = header.trim();
  if (/^\d+$/.test(value)) return Math.max(0, parseInt(value, 10));
  const at = Date.parse(value);
  if (Number.isNaN(at)) return undefined;
  return Math.max(0, Math.ceil((at - Date.now()) / 1000));
}

interface TimedJSONResponse {
  response: Response;
  body?: unknown;
  bodyError?: unknown;
}

/**
 * Keep one composed cancellation scope alive until the response body has been consumed.
 * `fetch()` resolves as soon as headers arrive, while a JSON body can still stall indefinitely.
 */
async function fetchJSONWithTimeout(
  url: string,
  options: RequestInit = {},
  timeoutMs: number = 45000
): Promise<TimedJSONResponse> {
  const controller = new AbortController();
  const callerSignal = options.signal;
  let abortCause: "caller" | "timeout" | null = null;

  const callerAbort = () => {
    if (abortCause) return;
    abortCause = "caller";
    controller.abort(callerSignal?.reason);
  };

  if (callerSignal?.aborted) {
    callerAbort();
  } else {
    callerSignal?.addEventListener("abort", callerAbort, { once: true });
  }
  const timeoutId = setTimeout(() => {
    if (abortCause) return;
    abortCause = "timeout";
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });

    if (response.status === 204) {
      return { response };
    }

    try {
      return { response, body: await response.json() };
    } catch (error) {
      // A malformed error body still carries a useful HTTP status. A caller abort or deadline,
      // however, must not be mistaken for malformed JSON merely because it happened after headers.
      if (abortCause) throw error;
      return { response, bodyError: error };
    }
  } catch (error) {
    if (abortCause === "timeout") {
      throw new APIError(`Request timeout after ${timeoutMs / 1000}s - server took too long to respond`, 504, 'Request timeout');
    }
    if (abortCause === "caller") {
      throw callerSignal?.reason ?? new DOMException("The request was cancelled", "AbortError");
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
    callerSignal?.removeEventListener("abort", callerAbort);
  }
}

/**
 * In-flight GET request deduplication.
 * Prevents duplicate concurrent GET requests to the same endpoint and auth generation.
 */
const _inflightGets = new Map<string, Promise<unknown>>();

// important: advanced when any write settles, and part of the de-duplication key. A read
// asked for after a write (a delete's refetch, the fleet read that follows it) used to join a
// GET that had started before the write and receive the pre-write body, which put a deleted
// row straight back on screen.
let _writeEpoch = 0;

export interface FetchAPIOptions {
  /**
   * Issue this request even if an identical GET is already in flight.
   *
   * important: set for anything a person explicitly asked for. Sharing a
   * promise with a poll that started before the click means the click never
   * reaches the server and the data that lands predates it, which is exactly
   * what made refresh buttons look broken.
   */
  force?: boolean;
  /**
   * Retry once after a same-actor token refresh. Disable this for non-idempotent writes whose
   * first outcome cannot safely be inferred from a lost or rewritten response.
   */
  retryAfterAuthRefresh?: boolean;
}

export async function fetchAPI<T>(
  endpoint: string,
  options: RequestInit = {},
  timeoutMs: number = 45000,
  _isRetry: boolean = false,
  fetchOptions: FetchAPIOptions = {},
): Promise<T> {
  const url = `${API_BASE_URL}${API_PREFIX}${endpoint}`;
  const method = (options.method || "GET").toUpperCase();
  const transportIsCallerScoped = options.signal !== undefined;
  const authGeneration = _authGeneration;
  const requestActorGeneration = _authActorGeneration;
  const inflightKey = `${authGeneration}:${_writeEpoch}:${endpoint}`;

  // Deduplicate concurrent GET requests only inside one installed auth context. Without the
  // generation, B can join A's still-pending promise for the same endpoint after an account
  // switch and receive A's response without issuing a request under B's bearer.
  if (method === "GET" && !_isRetry && !fetchOptions.force && !transportIsCallerScoped) {
    const existing = _inflightGets.get(inflightKey);
    if (existing) {
      return existing as Promise<T>;
    }
  }

  const request = _fetchAPIInternal<T>(
    endpoint,
    url,
    options,
    timeoutMs,
    _isRetry,
    requestActorGeneration,
    fetchOptions,
  );

  // Track in-flight GET requests for deduplication, keyed on auth generation and endpoint.
  // note: a forced read carries `?fresh=true`, so it keys separately from the poll of
  // the same resource and the two never join. That is the point: the click must reach
  // the server. `force` above covers the reads that cannot say so in the URL, where the
  // backend has no response cache to bypass.
  if (method === "GET" && !transportIsCallerScoped) {
    _inflightGets.set(inflightKey, request);
    request
      .finally(() => {
        // only clear our own entry - a forced request may have replaced it
        if (_inflightGets.get(inflightKey) === request) {
          _inflightGets.delete(inflightKey);
        }
      })
      // important: .finally() returns a NEW promise that rejects with the same reason, and
      // nothing awaits this one. Every failed GET therefore raised an unhandled rejection
      // on top of the error the caller was already handling. Invisible while no page load
      // produced a failing read, then three per mount the moment an endpoint answered 503.
      // The caller owns the rejection; this branch exists only for the bookkeeping.
      .catch(() => {});
  }

  if (method !== "GET" && method !== "HEAD") {
    // The epoch moves before the caller's continuation runs, so its onSuccess refetch is
    // already keyed past every read that predates the write.
    return request.finally(() => {
      _writeEpoch += 1;
    });
  }
  return request;
}

/**
 * Query-string suffix for a manual refresh.
 *
 * Manual clicks bypass the backend response cache; background polling does not.
 * A poll is high-frequency and low-intent, and making every open tab bypass the
 * cache would turn routine polling into a fan-out storm against live clusters.
 */
export function freshParam(endpoint: string, fresh?: boolean): string {
  if (!fresh) return endpoint;
  return endpoint.includes("?") ? `${endpoint}&fresh=true` : `${endpoint}?fresh=true`;
}

/**
 * Which response bodies the backend served past their cache TTL (X-Orcastra-Cache: stale).
 *
 * The backend answers a fan-out list from its last read for up to three minutes past the TTL
 * while one background read replaces it (backend core/swr.py). Left alone, the page would keep
 * that older body until its next poll, which for the fleet lists is three minutes away. The
 * QueryCache in components/providers.tsx asks again a few seconds later instead, when the
 * replacement has normally landed.
 *
 * note: keyed by the parsed body object, so a query function that reshapes the body (rather
 * than returning it) simply converges on its own poll, as before. Kept in this module because
 * the transport is compiled on its own by the structural checks.
 */
const staleBodies = new WeakSet<object>();

function noteServedStale(body: unknown, cacheHeader: string | null): void {
  if (cacheHeader === "stale" && body !== null && typeof body === "object") {
    staleBodies.add(body);
  }
}

export function wasServedStale(data: unknown): boolean {
  return data !== null && typeof data === "object" && staleBodies.has(data);
}

// The background read usually lands within a fan-out's duration (the instance list took 4.3 s
// at p50 and 6.7 s at p95 on dev), so the first look waits 5 s and the second 9 s more: a stale
// list is replaced within about 14 s at worst. Two looks per stale answer, never more: a cluster
// that stays down must not turn this into a poll.
export const STALE_FOLLOW_UP_DELAYS_MS = [5_000, 9_000] as const;

/** Options accepted by the read methods that back a refresh control. */
export interface ReadOptions {
  /** Bypass the backend response cache. Only ever set by a user action. */
  fresh?: boolean;
  /** Cancel the transport when the owning query is retired. */
  signal?: AbortSignal;
}

export interface InstanceListOptions extends ReadOptions {
  /** One LXD project; omitted, every project the caller may see. */
  project?: string;
}

// Endpoints that fan out across every cluster. Cached they are fast; forced
// fresh they are genuinely slow (/regions is 15-45s cold), and the default 45s
// budget turns that into a coin flip between an answer and a false timeout.
const HEAVY_FRESH_TIMEOUT_MS = 90000;

/** GET helper for the read paths behind a refresh button. */
export function fetchRead<T>(
  endpoint: string,
  opts: ReadOptions = {},
  timeoutMs?: number,
): Promise<T> {
  const target = freshParam(endpoint, opts.fresh);
  return fetchAPI<T>(
    target,
    { signal: opts.signal },
    timeoutMs,
    false,
    { force: opts.fresh === true },
  );
}

/** As fetchRead, but with the extended budget the fan-out endpoints need. */
function fetchHeavyRead<T>(endpoint: string, opts: ReadOptions = {}): Promise<T> {
  return fetchRead<T>(endpoint, opts, opts.fresh ? HEAVY_FRESH_TIMEOUT_MS : undefined);
}

/**
 * GET for a read whose backend keeps no response cache.
 *
 * important: `fresh` bypasses the in-flight dedupe, so a click landing during a poll still
 * reaches the server, but the query parameter is deliberately not sent. These handlers do
 * not read the response cache, so FastAPI dropped the unknown parameter while the
 * middleware still charged the request against the scarce per-operator bypass budget: the
 * click cost budget and changed nothing. Applies to /operations, /server, /identities,
 * /groups, /network-acls and /certificates.
 */
function fetchUncachedRead<T>(endpoint: string, opts: ReadOptions = {}): Promise<T> {
  return fetchAPI<T>(
    endpoint,
    { signal: opts.signal },
    undefined,
    false,
    { force: opts.fresh === true },
  );
}

async function _fetchAPIInternal<T>(
  endpoint: string,
  url: string,
  options: RequestInit,
  timeoutMs: number,
  _isRetry: boolean,
  requestActorGeneration: number,
  fetchOptions: FetchAPIOptions,
): Promise<T> {

  try {
    // build headers with auth token
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options.headers as Record<string, string>),
    };
    if (_accessToken) {
      headers["Authorization"] = `Bearer ${_accessToken}`;
    }

    const { response, body, bodyError } = await fetchJSONWithTimeout(url, {
      ...options,
      headers,
    }, timeoutMs);

    if (!response.ok) {
      let errorDetail = null;
      if (bodyError) {
        errorDetail = response.statusText;
      } else {
        const errorData = body as { detail?: unknown; error?: unknown } | null | undefined;
        errorDetail = errorData?.detail || errorData?.error;
      }
      
      // handle non-string error details (e.g. FastAPI validation errors return arrays)
      let errorMessage: string;
      if (!errorDetail) {
        errorMessage = `HTTP ${response.status}`;
      } else if (typeof errorDetail === 'string') {
        errorMessage = errorDetail;
      } else if (
        typeof errorDetail === 'object' &&
        errorDetail !== null &&
        typeof (errorDetail as { message?: unknown }).message === 'string'
      ) {
        // important: a structured refusal already carries a sentence written for a human. The
        // billing-impact gate and the break-glass service both answer with `{error, message, ...}`
        // so the caller can act on the machine-readable part, and stringifying the whole object
        // put a raw JSON blob in front of the operator instead of the sentence inside it.
        errorMessage = (errorDetail as { message: string }).message;
      } else {
        errorMessage = JSON.stringify(errorDetail);
      }

      // The same-origin proxy forwards upstream headers, so Retry-After reaches the browser.
      // A 429 used to surface as the bare detail; the picker notices count down from this.
      const retryAfter = parseRetryAfter(response.headers.get('Retry-After'));
      if (response.status === 429) {
        errorMessage = `Rate limited by the API. Try again in ${retryAfter ?? 30} s.`;
      }

      // On 401, trigger session refresh and retry once after token is refreshed
      if (response.status === 401 && typeof window !== 'undefined') {
        // A response from the previous actor is stale by definition. It must neither drive the
        // current actor's refresh state nor retry a mutation using the current actor's bearer.
        if (_authActorGeneration !== requestActorGeneration) {
          throw new APIError(errorMessage, response.status, errorDetail, retryAfter);
        }
        const refreshTriggered = handle401();

        // Retry once after waiting for session refresh to complete
        if (
          refreshTriggered &&
          !_isRetry &&
          fetchOptions.retryAfterAuthRefresh !== false
        ) {
          // Wait for the session refresh to complete (up to 21 s), but do not keep a query
          // alive after its key, actor, or component has been retired.
          await waitForSessionRefresh(options.signal);

          // A same-actor token rotation may retry. A direct A -> B transition may not: the
          // original request, including a mutation body, was authorised and intended by A.
          if (_authActorGeneration !== requestActorGeneration) {
            throw new APIError(errorMessage, response.status, errorDetail, retryAfter);
          }

          // Retry with the same actor's potentially refreshed token.
          return fetchAPI<T>(endpoint, options, timeoutMs, true, fetchOptions);
        }

        throw new APIError(errorMessage, response.status, errorDetail, retryAfter);
      }

      throw new APIError(errorMessage, response.status, errorDetail, retryAfter);
    }

    // a request got through, so whatever was failing has cleared
    if (
      _authActorGeneration === requestActorGeneration &&
      (_refreshCompleted || _authFailureAnnounced)
    ) {
      clearAuthFailureState();
    }

    // handle 204 No Content (no body to parse)
    if (response.status === 204) {
      return undefined as unknown as T;
    }

    if (bodyError) throw bodyError;
    noteServedStale(body, response.headers.get("X-Orcastra-Cache"));
    return body as T;
  } catch (error) {
    // Handle network errors or timeout
    if (error instanceof APIError) {
      throw error;
    }
    // Browsers disagree on whether an aborted fetch rejects with AbortError or TypeError.
    // Preserve query cancellation before the generic network-error mapping in either case.
    if (options.signal?.aborted) {
      throw options.signal.reason ?? new DOMException("The request was cancelled", "AbortError");
    }
    // TypeError from fetch indicates network-level failure (DNS, connection refused,
    // CORS, SSL, etc.). Message varies by browser: "Failed to fetch" (Chrome),
    // "NetworkError..." (Firefox), "Load failed" (Safari).
    if (error instanceof TypeError) {
      throw new APIError('Network error - unable to connect to server', 0, 'Connection failed');
    }
    throw error;
  }
}

// VM Operations
export const vmAPI = {
  list: async (
    host?: string,
    status?: string,
    offset?: number,
    limit?: number,
    opts: ReadOptions = {},
  ): Promise<VMListResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (status) params.append("status", status);
    if (offset && offset > 0) params.append("offset", String(offset));
    if (limit && limit > 0) params.append("limit", String(limit));
    
    const queryString = params.toString();
    const endpoint = queryString ? `/vms?${queryString}` : "/vms";

    return fetchRead<VMListResponse>(endpoint, opts);
  },

  get: async (name: string, host: string, project = "default"): Promise<VMInfo> => {
    const params = new URLSearchParams({ host, project });
    return fetchAPI<VMInfo>(
      `/vms/${encodeURIComponent(name)}?${params.toString()}`,
    );
  },

  create: async (data: VMCreateRequest): Promise<VMCreateResponse> => {
    return fetchAPI<VMCreateResponse>(
      "/vms",
      {
        method: "POST",
        body: JSON.stringify(data),
      },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    );
  },

  start: async (
    name: string,
    request: VMActionRequest
  ): Promise<VMActionResponse> => {
    return fetchAPI<VMActionResponse>(`/vms/${encodeURIComponent(name)}/start`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  stop: async (
    name: string,
    request: VMActionRequest
  ): Promise<VMActionResponse> => {
    return fetchAPI<VMActionResponse>(`/vms/${encodeURIComponent(name)}/stop`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  restart: async (
    name: string,
    request: VMActionRequest
  ): Promise<VMActionResponse> => {
    return fetchAPI<VMActionResponse>(`/vms/${encodeURIComponent(name)}/restart`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  delete: async (
    name: string,
    host: string,
    force = false,
    project?: string
  ): Promise<VMActionResponse> => {
    const params = new URLSearchParams({ host, force: String(force) });
    if (project) params.append("project", project);

    // A force delete stops a running VM first, which on a large guest can exceed
    // the default 45s. The abort is client-side only, so a premature timeout
    // leaves the delete running server-side (onSuccess never fires, the row
    // lingers, a retry 404s). Give it a generous window.
    return fetchAPI<VMActionResponse>(
      `/vms/${encodeURIComponent(name)}?${params.toString()}`,
      { method: "DELETE" },
      300000
    );
  },

  getMetrics: async (
    host: string,
    instanceName: string,
    project = "default"
  ): Promise<InstanceMetrics> => {
    return fetchAPI<InstanceMetrics>(
      `/vms/${encodeURIComponent(host)}/${encodeURIComponent(instanceName)}/metrics?project=${encodeURIComponent(project)}`
    );
  },
};

// Image Operations

/** Upper bound on the delete dialog's dependency preflight. See imageAPI.usage. */
const IMAGE_USAGE_TIMEOUT_MS = 8000;

export const imageAPI = {
  listAll: async (opts: ReadOptions = {}): Promise<ImageListResponse[]> => {
    return fetchRead<ImageListResponse[]>("/images", opts);
  },

  // `project` names the store to read: a project that keeps its own images answers with
  // them, a project that shares the host's store answers with the default store and says so
  // in `project`. Omitted, the backend reads the default store.
  listByHost: async (
    host: string,
    project?: string,
    opts: ReadOptions = {},
  ): Promise<ImageListResponse> => {
    const query = project ? `?project=${encodeURIComponent(project)}` : "";
    return fetchRead<ImageListResponse>(`/images/${host}${query}`, opts);
  },

  // Every store on the node the caller may see, each row naming its store. A separate call
  // rather than a flag on listByHost, whose `project` means "one store".
  listStores: async (host: string): Promise<ImageListResponse> => {
    return fetchAPI<ImageListResponse>(`/images/${encodeURIComponent(host)}?all_projects=true`);
  },

  listRemote: async (opts: ReadOptions = {}): Promise<RemoteImageListResponse> => {
    // The aggregated catalog can take ~30s on a cold cache (Ubuntu remote is
    // ~17 MB); allow a longer timeout than the 45s default.
    return fetchRead<RemoteImageListResponse>("/images/remote", opts, 120000);
  },

  // The authoritative dependent list, read before a delete is offered. The page's own count
  // comes from a tenant-filtered instance list and can undercount, so the confirm dialog must
  // not be built from it alone.
  //
  // important: a short timeout, not the 45s default. The confirm button waits on this, so a
  // slow or restarting backend would otherwise hold a destructive control disabled for the
  // best part of a minute with nothing to explain it. Eight seconds is long enough for a
  // fleet fan-out that is going to answer, and the caller falls back to what it can already
  // see. The backend refuses on its own either way, so giving up early is safe.
  usage: async (fingerprint: string, host: string, project?: string): Promise<ImageUsageResponse> => {
    const params = new URLSearchParams({ host });
    if (project) params.set("project", project);
    return fetchAPI<ImageUsageResponse>(
      `/images/${encodeURIComponent(fingerprint)}/usage?${params.toString()}`,
      {},
      IMAGE_USAGE_TIMEOUT_MS,
    );
  },

  // important: (host, project, fingerprint) is the key, not the fingerprint alone. An image
  // store is per cluster and a project may keep its own, so the same fingerprint can exist in
  // several and each row names one. `force` overrides the backend's refusal when instances
  // were built from the image; the backend, not this call, decides whether the refusal applies.
  delete: async (fingerprint: string, host: string, force = false, project?: string) => {
    const params = new URLSearchParams({ host });
    if (project) params.set("project", project);
    if (force) params.set("force", "true");
    return fetchAPI(`/images/${encodeURIComponent(fingerprint)}?${params.toString()}`, {
      method: "DELETE",
    });
  },
};

// Node Operations
export const nodeAPI = {
  status: async (opts: ReadOptions = {}): Promise<NodesStatusResponse> => {
    return fetchHeavyRead<NodesStatusResponse>("/nodes", opts);
  },

  names: async (opts: ReadOptions = {}): Promise<string[]> => {
    return fetchAPI<string[]>("/nodes/names", { signal: opts.signal });
  },

  // important: fetchRead, not fetchHeavyRead. Six pickers poll this; putting it on the
  // heavy-read budget alongside the node-status fan-out is the storm we are avoiding.
  availability: async (opts: ReadOptions = {}): Promise<ClusterAvailabilityResponse> => {
    return fetchRead<ClusterAvailabilityResponse>("/nodes/availability", opts);
  },

  resources: async (nodeName: string): Promise<NodeResources> => {
    return fetchAPI<NodeResources>(`/nodes/${nodeName}/resources`);
  },

  gpus: async (
    nodeName: string,
    opts: ReadOptions = {},
  ): Promise<GpuCardListResponse> => {
    return fetchAPI<GpuCardListResponse>(
      `/nodes/${encodeURIComponent(nodeName)}/gpus`,
      { signal: opts.signal },
    );
  },

  updateDisplayName: async (
    nodeName: string,
    displayName: string
  ): Promise<{ success: boolean; message: string; node_name: string; display_name: string }> => {
    return fetchAPI(
      `/nodes/${nodeName}`,
      {
        method: "PATCH",
        body: JSON.stringify({ display_name: displayName }),
      }
    );
  },

  invalidateCache: async (nodeName: string): Promise<{ success: boolean; message: string; node_name: string }> => {
    return fetchAPI(
      `/nodes/${nodeName}/cache/invalidate`,
      {
        method: "POST",
      }
    );
  },

  invalidateAllCache: async (): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(
      `/nodes/cache/invalidate-all`,
      {
        method: "POST",
      }
    );
  },
};

// Project Operations
export const projectAPI = {
  listAll: async (opts: ReadOptions = {}): Promise<ProjectListResponse[]> => {
    return fetchRead<ProjectListResponse[]>("/projects", opts);
  },

  list: async (host: string, opts: ReadOptions = {}): Promise<ProjectListResponse> => {
    return fetchRead<ProjectListResponse>(`/projects/${encodeURIComponent(host)}`, opts);
  },

  usage: async (
    host: string,
    project: string,
    opts: ReadOptions = {},
  ): Promise<ProjectUsageResponse> => {
    return fetchAPI<ProjectUsageResponse>(
      `/projects/${encodeURIComponent(host)}/${encodeURIComponent(project)}/usage`,
      { signal: opts.signal },
    );
  },

  create: async (data: ProjectCreateRequest): Promise<ProjectCreateResponse> => {
    return fetchAPI<ProjectCreateResponse>("/projects", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (host: string, projectName: string, data: {
    description?: string;
    max_cpu?: number;
    max_ram?: string;
    max_storage?: string;
  }): Promise<ProjectCreateResponse> => {
    return fetchAPI<ProjectCreateResponse>(
      `/projects/${host}/${encodeURIComponent(projectName)}`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      }
    );
  },

  delete: async (host: string, projectName: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI<{ success: boolean; message: string }>(
      `/projects/${host}/${encodeURIComponent(projectName)}`,
      { method: "DELETE" }
    );
  },
};

// Tags API (Enterprise-Grade)
export const tagAPI = {
  // ============================================
  // Category Operations
  // ============================================
  
  listCategories: async (includeInactive = false): Promise<import("@/types/api").TagCategory[]> => {
    return fetchAPI<import("@/types/api").TagCategory[]>(
      `/tags/categories?include_inactive=${includeInactive}`
    );
  },

  createCategory: async (
    data: import("@/types/api").TagCategoryCreate
  ): Promise<import("@/types/api").TagCategory> => {
    return fetchAPI<import("@/types/api").TagCategory>("/tags/categories", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  updateCategory: async (
    categoryId: number,
    data: import("@/types/api").TagCategoryUpdate
  ): Promise<import("@/types/api").TagCategory> => {
    return fetchAPI<import("@/types/api").TagCategory>(
      `/tags/categories/${categoryId}`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      }
    );
  },

  // ============================================
  // Tag CRUD Operations
  // ============================================

  listTags: async (params?: {
    category_id?: number;
    search?: string;
  }): Promise<import("@/types/api").TagListResponse> => {
    const searchParams = new URLSearchParams();
    if (params?.category_id) searchParams.append("category_id", String(params.category_id));
    if (params?.search) searchParams.append("search", params.search);
    const query = searchParams.toString();
    return fetchAPI<import("@/types/api").TagListResponse>(
      `/tags${query ? `?${query}` : ""}`
    );
  },

  createTag: async (
    data: import("@/types/api").TagCreate
  ): Promise<import("@/types/api").Tag> => {
    return fetchAPI<import("@/types/api").Tag>("/tags", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  updateTag: async (
    tagId: number,
    data: import("@/types/api").TagUpdate
  ): Promise<import("@/types/api").Tag> => {
    return fetchAPI<import("@/types/api").Tag>(`/tags/${tagId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  deleteTag: async (tagId: number): Promise<void> => {
    return fetchAPI<void>(`/tags/${tagId}`, { method: "DELETE" });
  },

  // ============================================
  // Statistics
  // ============================================

  getStats: async (): Promise<import("@/types/api").TagStatsResponse> => {
    return fetchAPI<import("@/types/api").TagStatsResponse>("/tags/stats");
  },

  // ============================================
  // Resource Tag Operations (New API)
  // ============================================

  getResourceTags: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    host?: string,
    project?: string
  ): Promise<import("@/types/api").ResourceTagsResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<import("@/types/api").ResourceTagsResponse>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}${query ? `?${query}` : ""}`
    );
  },

  setResourceTags: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    tags: string[],
    host?: string,
    project?: string
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}${query ? `?${query}` : ""}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: resourceType, resource_id: resourceId }),
      }
    );
  },

  addResourceTags: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    tags: string[],
    host?: string,
    project?: string
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}/add${query ? `?${query}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ tags, resource_type: resourceType, resource_id: resourceId }),
      }
    );
  },

  removeResourceTag: async (
    resourceType: "node" | "project" | "instance",
    resourceId: string,
    tagName: string,
    host?: string,
    project?: string
  ): Promise<void> => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    const query = params.toString();
    return fetchAPI<void>(
      `/tags/resources/${resourceType}/${encodeURIComponent(resourceId)}/${encodeURIComponent(tagName)}${query ? `?${query}` : ""}`,
      { method: "DELETE" }
    );
  },

  // ============================================
  // User Visibility Settings
  // ============================================

  getUserVisibility: async (): Promise<import("@/types/api").UserTagVisibilityResponse> => {
    return fetchAPI<import("@/types/api").UserTagVisibilityResponse>("/tags/visibility");
  },

  updateUserVisibility: async (
    settings: import("@/types/api").UserTagVisibilitySettings
  ): Promise<import("@/types/api").UserTagVisibilityResponse> => {
    return fetchAPI<import("@/types/api").UserTagVisibilityResponse>("/tags/visibility", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
  },

  // ============================================
  // Utility Endpoints
  // ============================================

  classifyTags: async (tags: string[]): Promise<import("@/types/api").TagGrouped> => {
    return fetchAPI<import("@/types/api").TagGrouped>("/tags/classify", {
      method: "POST",
      body: JSON.stringify(tags),
    });
  },

  validateTags: async (tags: string[]): Promise<Record<string, { valid: boolean; error: string | null }>> => {
    return fetchAPI<Record<string, { valid: boolean; error: string | null }>>("/tags/validate", {
      method: "POST",
      body: JSON.stringify(tags),
    });
  },

  initCategories: async (): Promise<{ success: boolean; message: string; created_count: number }> => {
    return fetchAPI<{ success: boolean; message: string; created_count: number }>(
      "/tags/init-categories",
      { method: "POST" }
    );
  },

  // ============================================
  // Legacy Endpoints (Backward Compatibility)
  // ============================================

  updateInstanceTags: async (
    host: string,
    instanceName: string,
    tags: string[],
    project: string = "default"
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/instances/${host}/${encodeURIComponent(instanceName)}?project=${project}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: "instance", resource_id: instanceName }),
      }
    );
  },

  updateProjectTags: async (
    host: string,
    projectName: string,
    tags: string[]
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/projects/${host}/${encodeURIComponent(projectName)}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: "project", resource_id: projectName }),
      }
    );
  },

  updateNodeTags: async (
    nodeName: string,
    tags: string[]
  ): Promise<import("@/types/api").BulkTagAssignResponse> => {
    return fetchAPI<import("@/types/api").BulkTagAssignResponse>(
      `/tags/nodes/${nodeName}`,
      {
        method: "PUT",
        body: JSON.stringify({ tags, resource_type: "node", resource_id: nodeName }),
      }
    );
  },

  getInstanceTags: async (
    host: string,
    instanceName: string,
    project: string = "default"
  ): Promise<import("@/types/api").ResourceTagsResponse> => {
    return fetchAPI<import("@/types/api").ResourceTagsResponse>(
      `/tags/instances/${host}/${encodeURIComponent(instanceName)}?project=${project}`
    );
  },
};

// Health Check
export const healthCheck = async (): Promise<{ status: string }> => {
  const response = await fetch("/health");
  if (!response.ok) {
    throw new APIError("Health check failed", response.status);
  }
  return response.json();
};

// System API
const systemAPI = {
  getSettings: async (): Promise<SystemSettings> => {
    return fetchAPI<SystemSettings>("/system/settings");
  },
  getVersion: async () => {
    return fetchAPI<{
      app_version: string;
      api_version: string;
      build_date?: string | null;
      build_commit?: string | null;
      environment: string;
    }>("/system/version");
  },
  getResourceUsage: async (opts: ReadOptions = {}): Promise<SystemResourceUsage> => {
    return fetchUncachedRead<SystemResourceUsage>("/system/resource-usage", opts);
  },
  clearCache: async (namespace?: string): Promise<{ message: string; namespaces: string[] }> => {
    const params = new URLSearchParams();
    if (namespace) params.append("namespace", namespace);

    const queryString = params.toString();
    return fetchAPI<{ message: string; namespaces: string[] }>(
      queryString ? `/system/cache/clear?${queryString}` : "/system/cache/clear",
      { method: "POST" }
    );
  },
};

// Instance Operations (similar to VMs but more comprehensive)
export interface RebuildRequest {
  host: string;
  project?: string;
  mode: "image" | "empty" | "cloud-init";
  image?: string;
  image_server?: string;
  image_protocol?: string;
  cloud_init?: string;
  confirm_name: string;
  run_provisioning?: boolean;
}

export type RebuildStatus =
  | "pending" | "stopping" | "rebuilding" | "starting" | "provisioning" | "ready" | "failed";

export interface RebuildJobStatus {
  job_id: string;
  instance_name: string;
  host: string;
  project: string;
  mode: string;
  requested_image: string | null;
  old_image: string | null;
  status: RebuildStatus;
  operation_id: string | null;
  error: string | null;
  attached_volumes: string[];
  created_at: string | null;
  updated_at: string | null;
}

export interface RebuildPreflight {
  instance_name: string;
  host: string;
  status: string;
  type: string;
  snapshots: string[];
  attached_volumes: string[];
  base_image: string | null;
}

export const instanceAPI = {
  getWritePolicy: async (
    host: string,
    opts: ReadOptions = {},
  ): Promise<InstanceWritePolicy> => {
    return fetchUncachedRead<InstanceWritePolicy>(
      withQuery("/instances/write-policy", { host }),
      { ...opts, fresh: true },
    );
  },

  preflightCreate: async (
    data: InstanceCreateRequest,
  ): Promise<InstanceCreatePreflightResponse> => {
    return fetchAPI<InstanceCreatePreflightResponse>("/instances/preflight", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  create: async (
    data: InstanceCreateRequest,
    idempotencyKey?: string,
  ): Promise<VMCreateResponse> => {
    return fetchAPI<VMCreateResponse>("/instances", {
      method: "POST",
      body: JSON.stringify(data),
      ...(idempotencyKey
        ? { headers: { "Idempotency-Key": idempotencyKey } }
        : {}),
    });
  },

  getOperation: async (
    jobId: string,
    read: ReadOptions = {},
  ): Promise<InstanceOperationJob> => {
    return fetchUncachedRead<InstanceOperationJob>(
      `/instances/operations/${encodeURIComponent(jobId)}`,
      { ...read, fresh: true },
    );
  },

  getSpecification: async (
    name: string,
    host: string,
    project = "default",
    opts: ReadOptions = {},
  ): Promise<InstanceSpecificationDetail> => {
    return fetchAPI<InstanceSpecificationDetail>(
      withQuery(`/instances/${encodeURIComponent(name)}/specification`, {
        host,
        project,
      }),
      { signal: opts.signal },
      undefined,
      false,
      { force: true },
    );
  },

  preflightSpecification: async (
    name: string,
    data: InstanceSpecificationPreflightRequest,
  ): Promise<InstanceSpecificationPreflightResponse> => {
    return fetchAPI<InstanceSpecificationPreflightResponse>(
      `/instances/${encodeURIComponent(name)}/specification/preflight`,
      { method: "POST", body: JSON.stringify(data) },
    );
  },

  updateSpecification: async (
    name: string,
    data: InstanceSpecificationUpdateRequest,
    etag: string,
    idempotencyKey: string,
  ): Promise<InstanceOperationJob> => {
    return fetchAPI<InstanceOperationJob>(
      `/instances/${encodeURIComponent(name)}/specification`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
        headers: {
          "If-Match": etag,
          "Idempotency-Key": idempotencyKey,
        },
      },
    );
  },

  specifications: async (
    opts: ReadOptions = {},
  ): Promise<InstanceSpecificationsResponse> => {
    return fetchRead<InstanceSpecificationsResponse>("/instances/specifications", opts);
  },

  // Read-only pre-check for the Rebuild dialog (snapshots block, volumes survive).
  rebuildPreflight: async (
    name: string,
    host: string,
    project = "default",
    opts: ReadOptions = {},
  ): Promise<RebuildPreflight> => {
    const p = new URLSearchParams({ host, project });
    return fetchAPI<RebuildPreflight>(
      `/instances/${encodeURIComponent(name)}/rebuild-preflight?${p.toString()}`,
      { signal: opts.signal },
    );
  },

  // Kick off a rebuild; returns the job to poll. Reinstalls the root disk while
  // preserving identity/config/attached volumes.
  rebuild: async (name: string, body: RebuildRequest): Promise<RebuildJobStatus> => {
    return fetchAPI<RebuildJobStatus>(
      `/instances/${encodeURIComponent(name)}/rebuild`,
      { method: "POST", body: JSON.stringify(body) },
      60000,
      false,
      { retryAfterAuthRefresh: false },
    );
  },

  getRebuildJob: async (
    name: string,
    jobId: string,
    opts: ReadOptions = {},
  ): Promise<RebuildJobStatus> => {
    return fetchAPI<RebuildJobStatus>(
      `/instances/${encodeURIComponent(name)}/rebuild/${encodeURIComponent(jobId)}`,
      { signal: opts.signal },
      undefined,
      false,
      { force: opts.fresh === true },
    );
  },

  // `project` narrows the list to one project (the backend keys its cache on it); omitted,
  // every project the caller may see. Passed through the trailing options so the five
  // positional callers stay as they are.
  list: async (host?: string, status?: string, type?: string, offset?: number, limit?: number, opts: InstanceListOptions = {}): Promise<VMListResponse> => {
    const { project, ...read } = opts;
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (project) params.append("project", project);
    if (status) params.append("status", status);
    if (type) params.append("instance_type", type);
    if (offset && offset > 0) params.append("offset", String(offset));
    if (limit && limit > 0) params.append("limit", String(limit));
    
    const queryString = params.toString();
    const endpoint = queryString ? `/instances?${queryString}` : "/instances";

    return fetchRead<VMListResponse>(endpoint, read);
  },

  // important: `project` is optional but should be passed whenever the caller knows it. An
  // instance name is unique per project, not per host, so without it the backend searches every
  // project and returns the first match, which can be a different instance of the same name.
  get: async (
    name: string,
    host: string,
    project?: string,
    opts: ReadOptions = {},
  ): Promise<VMInfo> => {
    const params = new URLSearchParams({ host });
    if (project) params.set("project", project);
    return fetchAPI<VMInfo>(
      `/instances/${encodeURIComponent(name)}?${params.toString()}`,
      { signal: opts.signal },
    );
  },
};

// Profile Operations

/**
 * Build `path?a=1&b=2`, dropping anything unset.
 *
 * The network, ACL and profile endpoints all carry an optional host and an optional LXD
 * project, and hand-assembling that in eleven call sites is how one of them ends up omitting
 * the project and silently reading the default one.
 */
function withQuery(path: string, params: Record<string, string | undefined>): string {
  const query = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => `${key}=${encodeURIComponent(value as string)}`)
    .join("&");
  return query ? `${path}?${query}` : path;
}

export const profileAPI = {
  /** Omit `project` to read every project, each row naming the one that holds it. */
  list: async (host?: string, project?: string, opts: ReadOptions = {}) => {
    return fetchRead(withQuery("/profiles", { host, project }), opts);
  },

  get: async (
    name: string,
    host: string,
    project?: string,
    opts: ReadOptions = {},
  ) => {
    return fetchAPI<ProfileDetail>(
      withQuery(`/profiles/${encodeURIComponent(name)}`, { host, project }),
      { signal: opts.signal },
    );
  },

  create: async (data: ProfileCreateRequest, host: string, project?: string) => {
    return fetchAPI<ProfileWriteResponse>(withQuery("/profiles", { host, project }), {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  /** `etag` is what the single read answered; with it the write is refused (412) if the profile changed since. */
  update: async (name: string, host: string, data: ProfileUpdateRequest, project?: string, etag?: string | null) => {
    return fetchAPI<ProfileWriteResponse>(withQuery(`/profiles/${encodeURIComponent(name)}`, { host, project }), {
      method: "PUT",
      body: JSON.stringify(data),
      ...(etag ? { headers: { "If-Match": etag } } : {}),
    });
  },

  delete: async (name: string, host: string, project?: string) => {
    return fetchAPI(withQuery(`/profiles/${encodeURIComponent(name)}`, { host, project }), {
      method: "DELETE",
    });
  },
};

// Network Operations
export const instanceDeviceAPI = {
  /**
   * Reserve a fixed IPv4 for one of an instance's interfaces, or clear it back to DHCP by
   * passing null. The backend validates the address against the network's real subnet.
   */
  setNicAddress: async (
    instanceName: string,
    deviceName: string,
    body: { host: string; project?: string; ipv4_address: string | null },
  ) => {
    return fetchAPI(
      `/instances/${encodeURIComponent(instanceName)}/devices/${encodeURIComponent(
        deviceName,
      )}/address`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
  },
};

export const networkAPI = {
  /** Omit `project` to read every project, each row naming the one that holds it. */
  list: async (host?: string, project?: string, opts: ReadOptions = {}) => {
    return fetchRead(withQuery("/networks", { host, project }), opts);
  },

  get: async (name: string, host: string, project?: string) => {
    return fetchAPI(withQuery(`/networks/${name}`, { host, project }));
  },

  getState: async (name: string, host: string, project?: string) => {
    return fetchAPI(withQuery(`/networks/${name}/state`, { host, project }));
  },

  create: async (data: any, host: string, project?: string) => {
    return fetchAPI(withQuery("/networks", { host, project }), {
      method: "POST",
      body: JSON.stringify(data),
    }, 195_000, false, { retryAfterAuthRefresh: false });
  },

  update: async (name: string, host: string, data: any, project?: string) => {
    return fetchAPI(withQuery(`/networks/${name}`, { host, project }), {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  delete: async (name: string, host: string, project?: string) => {
    return fetchAPI(withQuery(`/networks/${name}`, { host, project }), {
      method: "DELETE",
    });
  },
};

// Network ACL Operations
export const networkACLAPI = {
  /** Omit `project` to read every project, each row naming the one that holds it. */
  list: async (host?: string, project?: string, opts: ReadOptions = {}) => {
    return fetchUncachedRead(withQuery("/network-acls", { host, project }), opts);
  },

  get: async (name: string, host: string, project?: string) => {
    return fetchAPI<NetworkACLDetail>(withQuery(`/network-acls/${name}`, { host, project }));
  },

  create: async (data: any, host: string, project?: string) => {
    return fetchAPI(withQuery("/network-acls", { host, project }), {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  /** `etag` is what the single read answered; with it the write is refused (412) if the ACL changed since. */
  update: async (name: string, host: string, data: any, project?: string, etag?: string | null) => {
    return fetchAPI(withQuery(`/network-acls/${name}`, { host, project }), {
      method: "PUT",
      body: JSON.stringify(data),
      ...(etag ? { headers: { "If-Match": etag } } : {}),
    });
  },

  delete: async (name: string, host: string, project?: string) => {
    return fetchAPI(withQuery(`/network-acls/${name}`, { host, project }), {
      method: "DELETE",
    });
  },
};

// Storage Pool Operations
export const storagePoolAPI = {
  list: async (host?: string, opts: ReadOptions = {}) => {
    const endpoint = host ? `/storage/pools?host=${encodeURIComponent(host)}` : "/storage/pools";
    return fetchRead(endpoint, opts);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/storage/pools/${name}?host=${encodeURIComponent(host)}`);
  },

  getResources: async (name: string, host: string) => {
    return fetchAPI(`/storage/pools/${name}/resources?host=${encodeURIComponent(host)}`);
  },

  getResourcesAll: async (opts: ReadOptions = {}) => {
    return fetchHeavyRead("/storage/pools/resources-all", opts);
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/storage/pools?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (name: string, host: string, data: any) => {
    return fetchAPI(`/storage/pools/${name}?host=${encodeURIComponent(host)}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },

  delete: async (name: string, host: string) => {
    return fetchAPI(`/storage/pools/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Storage Volume Operations
export const storageVolumeAPI = {
  list: async (host?: string, pool?: string, opts: ReadOptions = {}) => {
    const params = new URLSearchParams();
    if (host) params.append("host", host);
    if (pool) params.append("pool", pool);
    
    const queryString = params.toString();
    const endpoint = queryString ? `/storage/volumes?${queryString}` : "/storage/volumes";

    return fetchRead(endpoint, opts);
  },

  get: async (volumeType: string, name: string, host: string, pool: string) => {
    return fetchAPI(
      `/storage/volumes/${volumeType}/${name}?host=${encodeURIComponent(host)}&pool=${encodeURIComponent(pool)}`
    );
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/storage/volumes?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (volumeType: string, name: string, host: string, pool: string, data: any) => {
    return fetchAPI(
      `/storage/volumes/${volumeType}/${name}?host=${encodeURIComponent(host)}&pool=${encodeURIComponent(pool)}`,
      {
        method: "PUT",
        body: JSON.stringify(data),
      }
    );
  },

  delete: async (volumeType: string, name: string, host: string, pool: string) => {
    return fetchAPI(
      `/storage/volumes/${volumeType}/${name}?host=${encodeURIComponent(host)}&pool=${encodeURIComponent(pool)}`,
      {
        method: "DELETE",
      }
    );
  },
};

// ISO upload (resumable, tus-style). Raw fetch is used for the binary PATCH so
// the file streams in chunks instead of being buffered as JSON.
const ISO_UPLOAD_BASE = `${API_BASE_URL}${API_PREFIX}/storage/iso-uploads`;

export interface UploadRequestOptions {
  signal?: AbortSignal;
  /** Stable, non-secret actor scope captured before the upload starts. */
  actorScope?: string;
}

export interface UploadAbortOptions {
  /** Stable, non-secret actor scope captured before the upload starts. */
  actorScope?: string;
  keepalive?: boolean;
}

function assertUploadActor(actorScope?: string): void {
  if (actorScope && !isAuthActorSynchronized(actorScope)) {
    throw new DOMException("Authenticated actor changed", "AbortError");
  }
}

function isoAuthHeaders(
  extra?: Record<string, string>,
  actorScope?: string,
): Record<string, string> {
  assertUploadActor(actorScope);
  const token = getAccessToken();
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(extra || {}),
  };
}

export interface IsoUploadCreateBody {
  cluster_id: string;
  project?: string;
  pool: string;
  volume_name: string;
  total_bytes: number;
  max_bytes: number;
  expected_sha256?: string;
  iso_ref?: string;
}

export interface IsoUploadStatus {
  session_id: string;
  status: "pending" | "uploading" | "uploaded" | "importing" | "ready" | "failed";
  offset: number;
  total_bytes: number;
  pool: string;
  volume_name: string;
  size: number | null;
  error: string | null;
}

async function isoErr(
  res: Response,
  fallback: string,
  parsedBody?: unknown,
): Promise<APIError> {
  let body = parsedBody;
  if (body === undefined) {
    try {
      body = await res.json();
    } catch {
      body = undefined;
    }
  }
  const errorBody = body as { detail?: unknown; error?: unknown } | undefined;
  const detail = errorBody?.detail ?? errorBody?.error;
  const message =
    typeof detail === "string"
      ? detail
      : detail && typeof detail === "object" &&
          typeof (detail as { message?: unknown }).message === "string"
        ? (detail as { message: string }).message
        : fallback;
  return new APIError(
    message,
    res.status,
    detail,
    parseRetryAfter(res.headers.get("Retry-After")),
  );
}

async function uploadJSON<T>(
  url: string,
  options: RequestInit,
  fallback: string,
): Promise<T> {
  const { response, body, bodyError } = await fetchJSONWithTimeout(url, options);
  if (!response.ok) throw await isoErr(response, fallback, body);
  if (bodyError) {
    throw new APIError(fallback, response.status, "Invalid JSON response");
  }
  return body as T;
}

export const isoUploadAPI = {
  create: async (
    body: IsoUploadCreateBody,
    opts: UploadRequestOptions = {},
  ): Promise<{ session_id: string; offset: number; expires_at: string }> => {
    return uploadJSON(ISO_UPLOAD_BASE, {
      method: "POST",
      headers: isoAuthHeaders({ "Content-Type": "application/json" }, opts.actorScope),
      body: JSON.stringify(body),
      signal: opts.signal,
    }, "Failed to start upload");
  },

  // Append one chunk at `offset`; returns the server's new offset (also handles a
  // 409 offset-mismatch by returning the authoritative offset so the caller resumes).
  patchChunk: async (
    sessionId: string,
    offset: number,
    chunk: Blob,
    opts: UploadRequestOptions = {},
  ): Promise<number> => {
    const { response: res, body } = await fetchJSONWithTimeout(
      `${ISO_UPLOAD_BASE}/${sessionId}`,
      {
        method: "PATCH",
        headers: isoAuthHeaders({
          "Content-Type": "application/offset+octet-stream",
          "Upload-Offset": String(offset),
        }, opts.actorScope),
        body: chunk,
        signal: opts.signal,
      },
      300_000,
    );
    if (res.status === 409) {
      return Number(res.headers.get("Upload-Offset") || String(offset));
    }
    if (!res.ok) throw await isoErr(res, `Chunk upload failed (${res.status})`, body);
    return Number(
      res.headers.get("Upload-Offset") || String(offset + chunk.size),
    );
  },

  finalize: async (
    sessionId: string,
    opts: UploadRequestOptions = {},
  ): Promise<IsoUploadStatus> => {
    return uploadJSON(`${ISO_UPLOAD_BASE}/${sessionId}/finalize`, {
      method: "POST",
      headers: isoAuthHeaders(undefined, opts.actorScope),
      signal: opts.signal,
    }, "Finalize failed");
  },

  status: async (
    sessionId: string,
    opts: UploadRequestOptions = {},
  ): Promise<IsoUploadStatus> => {
    return uploadJSON(`${ISO_UPLOAD_BASE}/${sessionId}`, {
      method: "GET",
      headers: isoAuthHeaders(undefined, opts.actorScope),
      signal: opts.signal,
    }, "Status check failed");
  },

  // Best-effort cancel: releases the server-side upload slot immediately instead
  // of waiting for the idle sweep/TTL. Never throws so it is safe to fire from
  // cancel/error/teardown paths. `keepalive` lets it survive a tab close/reload.
  abort: async (
    sessionId: string,
    opts: UploadAbortOptions = {},
  ): Promise<void> => {
    // An A-owned upload may not be deleted using B's newly installed bearer. The backend TTL
    // is the safe cleanup backstop once the initiating actor has retired.
    if (opts.actorScope && !isAuthActorSynchronized(opts.actorScope)) return;
    try {
      await fetch(`${ISO_UPLOAD_BASE}/${sessionId}`, {
        method: "DELETE",
        headers: isoAuthHeaders(undefined, opts.actorScope),
        keepalive: opts.keepalive ?? true,
      });
    } catch {
      /* nothing to do: the idle sweep and TTL are the backstop */
    }
  },
};

// Custom-image upload (resumable, tus-style), the direct analogue of
// isoUploadAPI, hitting the JWT image-upload router. A unified LXD image file is
// streamed in chunks and imported into the cluster's image store under a private
// alias.
const IMAGE_UPLOAD_BASE = `${API_BASE_URL}${API_PREFIX}/images/uploads`;

export interface ImageUploadCreateBody {
  cluster_id: string;
  project?: string;
  alias: string;
  instance_type: "container" | "virtual-machine";
  total_bytes: number;
  max_bytes: number;
  expected_sha256?: string;
  image_ref?: string;
}

export interface ImageUploadStatus {
  session_id: string;
  status: "pending" | "uploading" | "uploaded" | "importing" | "ready" | "failed";
  offset: number;
  total_bytes: number;
  fingerprint: string | null;
  size: number | null;
  alias: string;
  error: string | null;
}

export const imageUploadAPI = {
  create: async (
    body: ImageUploadCreateBody,
    opts: UploadRequestOptions = {},
  ): Promise<{ session_id: string; offset: number; expires_at: string }> => {
    return uploadJSON(IMAGE_UPLOAD_BASE, {
      method: "POST",
      headers: isoAuthHeaders({ "Content-Type": "application/json" }, opts.actorScope),
      body: JSON.stringify(body),
      signal: opts.signal,
    }, "Failed to start upload");
  },

  // Append one chunk at `offset`; returns the server's new offset (also handles a
  // 409 offset-mismatch by returning the authoritative offset so the caller resumes).
  patchChunk: async (
    sessionId: string,
    offset: number,
    chunk: Blob,
    opts: UploadRequestOptions = {},
  ): Promise<number> => {
    const { response: res, body } = await fetchJSONWithTimeout(
      `${IMAGE_UPLOAD_BASE}/${sessionId}`,
      {
        method: "PATCH",
        headers: isoAuthHeaders({
          "Content-Type": "application/offset+octet-stream",
          "Upload-Offset": String(offset),
        }, opts.actorScope),
        body: chunk,
        signal: opts.signal,
      },
      300_000,
    );
    if (res.status === 409) {
      return Number(res.headers.get("Upload-Offset") || String(offset));
    }
    if (!res.ok) throw await isoErr(res, `Chunk upload failed (${res.status})`, body);
    return Number(
      res.headers.get("Upload-Offset") || String(offset + chunk.size),
    );
  },

  finalize: async (
    sessionId: string,
    opts: UploadRequestOptions = {},
  ): Promise<ImageUploadStatus> => {
    return uploadJSON(`${IMAGE_UPLOAD_BASE}/${sessionId}/finalize`, {
      method: "POST",
      headers: isoAuthHeaders(undefined, opts.actorScope),
      signal: opts.signal,
    }, "Finalize failed");
  },

  status: async (
    sessionId: string,
    opts: UploadRequestOptions = {},
  ): Promise<ImageUploadStatus> => {
    return uploadJSON(`${IMAGE_UPLOAD_BASE}/${sessionId}`, {
      method: "GET",
      headers: isoAuthHeaders(undefined, opts.actorScope),
      signal: opts.signal,
    }, "Status check failed");
  },

  abort: async (
    sessionId: string,
    opts: UploadAbortOptions = {},
  ): Promise<void> => {
    if (opts.actorScope && !isAuthActorSynchronized(opts.actorScope)) return;
    try {
      await fetch(`${IMAGE_UPLOAD_BASE}/${sessionId}`, {
        method: "DELETE",
        headers: isoAuthHeaders(undefined, opts.actorScope),
        keepalive: opts.keepalive ?? true,
      });
    } catch {
      /* nothing to do: the idle sweep and TTL are the backstop */
    }
  },
};


// Warning Operations
export const warningAPI = {
  // note: fetchRead, not fetchUncachedRead. The backend caches both warning feeds per scope
  // now, so a refresh has to send fresh=true to reach the clusters.
  list: async (host?: string, opts: ReadOptions = {}) => {
    const endpoint = host ? `/warnings?host=${encodeURIComponent(host)}` : "/warnings";
    return fetchRead(endpoint, opts);
  },

  get: async (uuid: string, host: string) => {
    return fetchAPI(`/warnings/${uuid}?host=${encodeURIComponent(host)}`);
  },

  delete: async (uuid: string, host: string) => {
    return fetchAPI(`/warnings/${uuid}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Server Operations
export const serverAPI = {
  list: async (host?: string, opts: ReadOptions = {}) => {
    const endpoint = host ? `/server?host=${encodeURIComponent(host)}` : "/server";
    return fetchUncachedRead(endpoint, opts);
  },

  get: async (nodeName: string) => {
    return fetchAPI(`/server/${nodeName}`);
  },

  getResources: async (nodeName: string) => {
    return fetchAPI(`/server/${nodeName}/resources`);
  },

  updateConfig: async (nodeName: string, config: any) => {
    return fetchAPI(`/server/${nodeName}/config`, {
      method: "PUT",
      body: JSON.stringify(config),
    });
  },
};

// Identity Operations
export const identityAPI = {
  list: async (host?: string, opts: ReadOptions = {}) => {
    const endpoint = host ? `/identities?host=${encodeURIComponent(host)}` : "/identities";
    return fetchUncachedRead(endpoint, opts);
  },

  get: async (id: string, host: string) => {
    return fetchAPI(`/identities/${id}?host=${encodeURIComponent(host)}`);
  },

  delete: async (authType: string, identityId: string, host: string) => {
    return fetchAPI(
      `/identities/${authType}/${encodeURIComponent(identityId)}?host=${encodeURIComponent(host)}`,
      { method: "DELETE" }
    );
  },
};

// Group Operations
export const groupAPI = {
  list: async (host?: string, opts: ReadOptions = {}) => {
    const endpoint = host ? `/groups?host=${encodeURIComponent(host)}` : "/groups";
    return fetchUncachedRead(endpoint, opts);
  },

  get: async (name: string, host: string) => {
    return fetchAPI(`/groups/${name}?host=${encodeURIComponent(host)}`);
  },

  create: async (data: any, host: string) => {
    return fetchAPI(`/groups?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  update: async (name: string, data: any, host: string) => {
    return fetchAPI(`/groups/${name}?host=${encodeURIComponent(host)}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  addIdentity: async (groupName: string, authType: "tls" | "oidc", identityId: string, host: string) => {
    return fetchAPI(`/groups/${groupName}/identities?host=${encodeURIComponent(host)}`, {
      method: "POST",
      body: JSON.stringify({ auth_type: authType, identity_id: identityId }),
    });
  },

  removeIdentity: async (groupName: string, authType: string, identityId: string, host: string) => {
    return fetchAPI(
      `/groups/${groupName}/identities/${authType}/${encodeURIComponent(identityId)}?host=${encodeURIComponent(host)}`,
      {
        method: "DELETE",
      }
    );
  },

  delete: async (name: string, host: string) => {
    return fetchAPI(`/groups/${name}?host=${encodeURIComponent(host)}`, {
      method: "DELETE",
    });
  },
};

// Cluster Operations
export const clusterAPI = {
  register: async (data: ClusterRegisterRequest): Promise<ClusterRegisterResponse> => {
    return fetchAPI<ClusterRegisterResponse>("/clusters/register", {
      method: "POST",
      body: JSON.stringify(data),
    }, 90000); // 90 second timeout for Vault operations and auto-detect
  },

  checkAuth: async (clusterId: string): Promise<ClusterAuthCheckResponse> => {
    return fetchAPI<ClusterAuthCheckResponse>("/clusters/check-auth", {
      method: "POST",
      body: JSON.stringify({ cluster_id: clusterId }),
    }, 20000); // 20s timeout - node should respond quickly if trust is configured
  },

  list: async (
    fresh: boolean = false,
    opts: ReadOptions = {},
  ): Promise<ClusterListResponse> => {
    return fetchRead<ClusterListResponse>("/clusters", {
      ...opts,
      fresh: fresh || opts.fresh === true,
    });
  },

  get: async (clusterId: string): Promise<ClusterInfo> => {
    return fetchAPI<ClusterInfo>(`/clusters/${clusterId}`);
  },

  delete: async (clusterId: string): Promise<ClusterDeleteResponse> => {
    return fetchAPI<ClusterDeleteResponse>(`/clusters/${clusterId}`, {
      method: "DELETE",
    });
  },

  regenerateCert: async (clusterId: string): Promise<ClusterRegisterResponse> => {
    return fetchAPI<ClusterRegisterResponse>(
      `/clusters/${clusterId}/regenerate-cert`,
      {
        method: "POST",
      }
    );
  },

  updateLocation: async (clusterId: string, location: GeoLocation): Promise<ClusterLocationUpdateResponse> => {
    return fetchAPI<ClusterLocationUpdateResponse>(
      `/clusters/${clusterId}/location`,
      {
        method: "PATCH",
        body: JSON.stringify({ location }),
      }
    );
  },

  getFacilities: async (): Promise<Array<{ facility_name: string; facility_provider?: string }>> => {
    return fetchAPI<Array<{ facility_name: string; facility_provider?: string }>>("/clusters/facilities");
  },
};

// IPAM API
export const ipamAPI = {
  list: async (host?: string, opts: ReadOptions = {}) => {
    const url = host ? `/ipam?host=${encodeURIComponent(host)}` : "/ipam";
    // three all-projects reads plus one per project per node: the heavy budget on a fresh read
    return fetchHeavyRead<IPAMListResponse>(url, opts);
  },
};

// My Keys API
export const myKeysAPI = {
  list: async (statusFilter?: string, search?: string, opts: ReadOptions = {}) => {
    let url = "/my-keys";
    const params = new URLSearchParams();
    if (statusFilter) params.append("status_filter", statusFilter);
    if (search) params.append("search", search);
    if (params.toString()) url += `?${params.toString()}`;
    return fetchRead(url, opts);
  },

  // The backend answers with the KeyInfo it will list (routers/my_keys.py `response_model`),
  // which is what the list uses to bring the new row into view.
  create: async (request: KeyCreateRequest): Promise<KeyInfo> => {
    return fetchAPI<KeyInfo>("/my-keys", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  download: async (keyId: string) => {
    const url = `${API_BASE_URL}${API_PREFIX}/my-keys/${keyId}/download`;
    const response = await fetch(url, {
      headers: {
        ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
      },
    });
    if (!response.ok) {
      throw new Error("Failed to download key");
    }
    return response.blob();
  },

  delete: async (keyId: string) => {
    return fetchAPI(`/my-keys/${keyId}`, {
      method: "DELETE",
    });
  },
};

// Monitoring API
import type { 
  MonitoringDashboardResponse, 
  ClusterMonitoringMetrics, 
  MetricsHistoryPoint,
  CertificateListResponse,
  CertificateInfo,
  CertificateRenewalRequest,
  CertificateRenewalResponse,
  CertificateVerifyRequest,
  CertificateVerifyResponse,
  PendingCertificateResponse,
  SystemWarningsResponse,
  TrustCleanupResponse,
} from "@/types";

export const monitoringAPI = {
  getDashboard: async (opts: ReadOptions = {}): Promise<MonitoringDashboardResponse> => {
    return fetchRead<MonitoringDashboardResponse>("/monitoring/dashboard", opts);
  },

  getNodeMetrics: async (nodeName: string): Promise<ClusterMonitoringMetrics> => {
    return fetchAPI<ClusterMonitoringMetrics>(`/monitoring/nodes/${encodeURIComponent(nodeName)}/metrics`);
  },

  /** History collected in the last `windowSeconds`, whatever the collection cadence was. */
  getHistory: async (windowSeconds: number = 300): Promise<MetricsHistoryPoint[]> => {
    return fetchAPI<MetricsHistoryPoint[]>(`/monitoring/history?window=${windowSeconds}`);
  },
};

// Certificate Management API
export const certificateAPI = {
  list: async (opts: ReadOptions = {}): Promise<CertificateListResponse> => {
    return fetchUncachedRead<CertificateListResponse>("/certificates", opts);
  },

  get: async (clusterId: string): Promise<CertificateInfo> => {
    return fetchAPI<CertificateInfo>(`/certificates/${encodeURIComponent(clusterId)}`);
  },

  initiateRenewal: async (request: CertificateRenewalRequest): Promise<CertificateRenewalResponse> => {
    return fetchAPI<CertificateRenewalResponse>("/certificates/renew", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  verifyRenewal: async (request: CertificateVerifyRequest): Promise<CertificateVerifyResponse> => {
    return fetchAPI<CertificateVerifyResponse>("/certificates/verify", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  cancelPending: async (clusterId: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(`/certificates/${encodeURIComponent(clusterId)}/pending`, {
      method: "DELETE",
    });
  },

  /** The pending certificate, its fingerprint and the trust command, to resume Verify. */
  getPending: async (clusterId: string): Promise<PendingCertificateResponse> => {
    return fetchUncachedRead<PendingCertificateResponse>(
      `/certificates/${encodeURIComponent(clusterId)}/pending`,
    );
  },

  /** Remove superseded trust entries now; without a fingerprint, every open one on the cluster. */
  removeTrust: async (clusterId: string, fingerprint?: string): Promise<TrustCleanupResponse> => {
    return fetchAPI<TrustCleanupResponse>(
      `/certificates/${encodeURIComponent(clusterId)}/trust-cleanup`,
      { method: "POST", body: JSON.stringify(fingerprint ? { fingerprint } : {}) },
    );
  },

  getSystemWarnings: async (opts: ReadOptions = {}): Promise<SystemWarningsResponse> => {
    return fetchRead<SystemWarningsResponse>("/warnings/system/all", opts);
  },

  cleanupPending: async (): Promise<{ success: boolean; message: string; cleaned_count: number }> => {
    return fetchAPI("/certificates/cleanup-pending", {
      method: "POST",
    });
  },
};

// Integrations API (External Apps - Orcahub)
import type {
  APIKeyInfo,
  APIKeyCapabilitiesRequest,
  APIKeyCreateRequest,
  APIKeyCreateResponse,
  APIKeyListResponse,
  ClusterPermission,
  ExternalUserMapping,
  UserMappingRequest,
  AvailableClustersResponse,
  APIKeyValidateRequest,
  APIKeyValidateResponse,
  CapabilityVocabularyResponse,
  IntegrationOrganization,
  OwnerKeyTransferRequest,
  OwnerKeyTransferResponse,
  OwnerKeyChangeRequest,
  OwnerSetResponse,
} from "@/types";

export const integrationsAPI = {
  // API Key Management
  listAPIKeys: async (opts: ReadOptions = {}): Promise<APIKeyListResponse> => {
    return fetchRead<APIKeyListResponse>("/integrations/api-keys", opts);
  },

  getAPIKey: async (keyId: string): Promise<APIKeyInfo> => {
    return fetchAPI<APIKeyInfo>(`/integrations/api-keys/${encodeURIComponent(keyId)}`);
  },

  createAPIKey: async (request: APIKeyCreateRequest): Promise<APIKeyCreateResponse> => {
    return fetchAPI<APIKeyCreateResponse>("/integrations/api-keys", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  updateAPIKeyPermissions: async (keyId: string, permissions: ClusterPermission[]): Promise<APIKeyInfo> => {
    return fetchAPI<APIKeyInfo>(`/integrations/api-keys/${encodeURIComponent(keyId)}/permissions`, {
      method: "PUT",
      body: JSON.stringify(permissions),
    });
  },

  // note: withdrawing tenant.provision from a key that owns organizations answers 409 with what would
  // stop; the same request with force set goes through.
  updateAPIKeyCapabilities: async (keyId: string, request: APIKeyCapabilitiesRequest): Promise<APIKeyInfo> => {
    return fetchAPI<APIKeyInfo>(`/integrations/api-keys/${encodeURIComponent(keyId)}/capabilities`, {
      method: "PUT",
      body: JSON.stringify(request),
    });
  },

  revokeAPIKey: async (keyId: string): Promise<APIKeyInfo> => {
    return fetchAPI<APIKeyInfo>(`/integrations/api-keys/${encodeURIComponent(keyId)}/revoke`, {
      method: "POST",
    });
  },

  // note: a new secret for the same key id. The organizations the key owns record that id, so
  // they keep syncing once the integration is given the new secret.
  rotateAPIKeySecret: async (keyId: string): Promise<APIKeyCreateResponse> => {
    return fetchAPI<APIKeyCreateResponse>(`/integrations/api-keys/${encodeURIComponent(keyId)}/rotate-secret`, {
      method: "POST",
    });
  },

  deleteAPIKey: async (keyId: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(`/integrations/api-keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
    });
  },

  // User Mappings
  listUserMappings: async (keyId: string): Promise<ExternalUserMapping[]> => {
    return fetchAPI<ExternalUserMapping[]>(`/integrations/api-keys/${encodeURIComponent(keyId)}/users`);
  },

  addUserMapping: async (keyId: string, request: UserMappingRequest): Promise<ExternalUserMapping> => {
    return fetchAPI<ExternalUserMapping>(`/integrations/api-keys/${encodeURIComponent(keyId)}/users`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  deleteUserMapping: async (keyId: string, userId: string): Promise<{ success: boolean; message: string }> => {
    return fetchAPI(`/integrations/api-keys/${encodeURIComponent(keyId)}/users/${encodeURIComponent(userId)}`, {
      method: "DELETE",
    });
  },

  // Available Clusters
  getAvailableClusters: async (): Promise<AvailableClustersResponse> => {
    return fetchAPI<AvailableClustersResponse>("/integrations/available-clusters");
  },

  // The capability vocabulary is served rather than hardcoded so the picker cannot offer a
  // capability the external router does not enforce, or miss one it does.
  getCapabilities: async (): Promise<CapabilityVocabularyResponse> => {
    return fetchAPI<CapabilityVocabularyResponse>("/integrations/capabilities");
  },

  // note: this is the one integrations call that authenticates with the credential in the BODY
  // rather than with a session, because it is the endpoint an external app uses before it has
  // anything else. The dialog uses it to prove a freshly issued key works before the operator
  // walks away from the only screen that will ever show the secret.
  validate: async (request: APIKeyValidateRequest): Promise<APIKeyValidateResponse> => {
    return fetchAPI<APIKeyValidateResponse>("/integrations/validate", {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  // Synced Organizations from external apps
  listOrganizations: async (source?: string): Promise<IntegrationOrganization[]> => {
    const params = source ? `?source=${encodeURIComponent(source)}` : "";
    return fetchAPI<IntegrationOrganization[]>(`/integrations/organizations${params}`);
  },

  refreshOrganizations: async (apiKeyId: string): Promise<{ message: string; organizations_synced: number }> => {
    return fetchAPI(`/integrations/refresh-organizations?api_key_id=${encodeURIComponent(apiKeyId)}`, {
      method: "POST",
    });
  },

  // Admin only. Makes another active OrcaHub key the owner of a synced organization; there is no
  // release to no owner.
  transferOrganizationOwner: async (
    orgId: number,
    request: OwnerKeyTransferRequest,
  ): Promise<OwnerKeyTransferResponse> => {
    return fetchAPI<OwnerKeyTransferResponse>(`/integrations/organizations/${encodeURIComponent(String(orgId))}/owner-key`, {
      method: "PUT",
      body: JSON.stringify(request),
    });
  },

  // Admin only, and 404 unless INTEGRATION_MULTI_OWNER_ENABLED is on. Add a key to, remove one from,
  // or choose the primary of a synced organization's owner set.
  addOrganizationOwner: async (orgId: number, request: OwnerKeyChangeRequest): Promise<OwnerSetResponse> => {
    return fetchAPI<OwnerSetResponse>(`/integrations/organizations/${encodeURIComponent(String(orgId))}/owner-keys`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  },

  removeOrganizationOwner: async (orgId: number, apiKeyId: string, reason: string): Promise<OwnerSetResponse> => {
    const query = `?reason=${encodeURIComponent(reason)}`;
    return fetchAPI<OwnerSetResponse>(
      `/integrations/organizations/${encodeURIComponent(String(orgId))}/owner-keys/${encodeURIComponent(apiKeyId)}${query}`,
      { method: "DELETE" },
    );
  },

  setOrganizationPrimary: async (orgId: number, request: OwnerKeyChangeRequest): Promise<OwnerSetResponse> => {
    return fetchAPI<OwnerSetResponse>(`/integrations/organizations/${encodeURIComponent(String(orgId))}/primary-key`, {
      method: "PUT",
      body: JSON.stringify(request),
    });
  },
};

// Regions API
import { RegionsOverviewResponse, RegionSummary } from "@/types/api";

const regionsAPI = {
  getOverview: async (opts: ReadOptions = {}) => {
    return fetchHeavyRead<RegionsOverviewResponse>("/regions", opts);
  },
  getRegion: async (regionId: string) => {
    return fetchAPI<RegionSummary>(`/regions/${encodeURIComponent(regionId)}`);
  },
};

// Break-Glass API (least-privilege admin access to tenant console/terminal)
export interface BreakGlassCheck {
  required: boolean;
  has_active_grant: boolean;
  cluster_id: string;
  target_organization_id: number | null;
  target_organization_slug: string | null;
  grant_expires_at?: string | null;
}
export type BreakGlassGrantStatus = "active" | "expired" | "revoked";
export interface BreakGlassGrant {
  id: number;
  admin_email: string;
  cluster_id: string;
  target_organization_id: number | null;
  target_org_slug: string | null;
  reason: string;
  ticket_ref: string | null;
  status: BreakGlassGrantStatus;
  created_by: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  revoked_by: string | null;
}
const breakGlassAPI = {
  check: async (clusterId: string, opts: ReadOptions = {}): Promise<BreakGlassCheck> => {
    return fetchAPI<BreakGlassCheck>(
      `/break-glass/check?cluster_id=${encodeURIComponent(clusterId)}`,
      { signal: opts.signal },
    );
  },
  listGrants: async (mine = false): Promise<BreakGlassGrant[]> => {
    return fetchAPI<BreakGlassGrant[]>(`/break-glass/grants${mine ? "?mine=true" : ""}`);
  },
  createGrant: async (
    body: { cluster_id: string; reason: string; ticket_ref?: string; duration_minutes?: number },
    opts: { signal?: AbortSignal } = {},
  ): Promise<BreakGlassGrant> => {
    return fetchAPI<BreakGlassGrant>(
      "/break-glass/grants",
      { method: "POST", body: JSON.stringify(body), signal: opts.signal },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    );
  },
  revokeGrant: async (id: number): Promise<BreakGlassGrant> => {
    return fetchAPI<BreakGlassGrant>(`/break-glass/grants/${id}/revoke`, { method: "POST" });
  },
};

/**
 * Orcastra's own recurring snapshots, which are a different mechanism from the LXD-native schedule
 * `SnapshotPolicy` reports and never write it.
 */
const snapshotScheduleAPI = {
  list: async (
    filters: { host?: string; project?: string; instance?: string } = {},
    opts: ReadOptions = {},
  ): Promise<SnapshotScheduleListResponse> => {
    const qs = new URLSearchParams();
    // Omitted rather than sent blank, for the reason listFleet records: the backend reads a
    // present-but-empty name as a caller who named something and got it wrong.
    if (filters.host) qs.set("host", filters.host);
    if (filters.project) qs.set("project", filters.project);
    if (filters.instance) qs.set("instance", filters.instance);
    const query = qs.toString();
    return fetchRead<SnapshotScheduleListResponse>(
      query ? `/snapshot-schedules?${query}` : "/snapshot-schedules",
      opts,
    );
  },
  create: async (
    body: SnapshotScheduleCreateRequest,
  ): Promise<SnapshotScheduleActionResponse> =>
    fetchAPI<SnapshotScheduleActionResponse>("/snapshot-schedules", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  update: async (
    id: number,
    body: SnapshotScheduleUpdateRequest,
  ): Promise<SnapshotScheduleActionResponse> =>
    fetchAPI<SnapshotScheduleActionResponse>(`/snapshot-schedules/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  pause: async (id: number): Promise<SnapshotScheduleActionResponse> =>
    fetchAPI<SnapshotScheduleActionResponse>(`/snapshot-schedules/${id}/pause`, { method: "POST" }),
  resume: async (id: number): Promise<SnapshotScheduleActionResponse> =>
    fetchAPI<SnapshotScheduleActionResponse>(`/snapshot-schedules/${id}/resume`, { method: "POST" }),
  runNow: async (id: number): Promise<SnapshotScheduleActionResponse> =>
    fetchAPI<SnapshotScheduleActionResponse>(
      `/snapshot-schedules/${id}/run-now`,
      { method: "POST" },
      undefined,
      false,
      // The daemon snapshot creation is not idempotent. Never replay an ambiguous submission.
      { retryAfterAuthRefresh: false },
    ),
  remove: async (id: number): Promise<SnapshotScheduleActionResponse> =>
    fetchAPI<SnapshotScheduleActionResponse>(`/snapshot-schedules/${id}`, { method: "DELETE" }),
};

const snapshotAPI = {
  /** Snapshots on one instance, with the source's schedule for context. */
  list: async (
    instanceName: string,
    host: string,
    project = "default",
    opts: ReadOptions = {},
  ): Promise<SnapshotListResponse> => {
    const qs = `host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`;
    return fetchRead<SnapshotListResponse>(
      `/instances/${encodeURIComponent(instanceName)}/snapshots?${qs}`,
      opts,
    );
  },
  /**
   * Every snapshot in scope, one upstream call per cluster, then a bounded size read per holder.
   *
   * Both parameters are omitted rather than sent empty when they are not set. The backend reads
   * a present-but-blank host as a caller who named a cluster and got it wrong, and answers 422,
   * which is what stops a narrowed request silently widening into a fleet sweep.
   *
   * A heavy read: the fan-out runs on a 40 s backend budget, and the default window leaves a
   * forced refresh no headroom.
   */
  listFleet: async (
    host?: string,
    project?: string,
    opts: ReadOptions = {},
  ): Promise<FleetSnapshotListResponse> => {
    const qs = new URLSearchParams();
    if (host) qs.set("host", host);
    if (project) qs.set("project", project);
    const query = qs.toString();
    return fetchHeavyRead<FleetSnapshotListResponse>(
      query ? `/snapshots?${query}` : "/snapshots",
      opts,
    );
  },
  create: async (
    instanceName: string,
    body: SnapshotCreateRequest,
  ): Promise<SnapshotCreateResponse> => {
    return fetchAPI<SnapshotCreateResponse>(
      `/instances/${encodeURIComponent(instanceName)}/snapshots`,
      { method: "POST", body: JSON.stringify(body) },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    );
  },
  remove: async (
    instanceName: string,
    snapshotName: string,
    host: string,
    project = "default",
    opts: {
      reason?: string;
      acknowledgeBillingImpact?: boolean;
      acknowledgeUpstreamSnapshot?: boolean;
      confirmSnapshot?: string;
    } = {},
  ): Promise<SnapshotActionResponse> => {
    // DELETE carries no body, so every gate travels as a query parameter. Deliberately not moved
    // to a body: a DELETE body is not carried through every proxy, and a confirmation something
    // can silently strip fails open in exactly the direction these gates close.
    const params = new URLSearchParams({ host, project });
    if (opts.acknowledgeBillingImpact) params.set("acknowledge_billing_impact", "true");
    if (opts.acknowledgeUpstreamSnapshot) params.set("acknowledge_upstream_snapshot", "true");
    if (opts.acknowledgeBillingImpact || opts.acknowledgeUpstreamSnapshot) {
      params.set("reason", opts.reason ?? "");
    }
    if (opts.confirmSnapshot) params.set("confirm_snapshot", opts.confirmSnapshot);
    const qs = params.toString();
    return fetchAPI<SnapshotActionResponse>(
      `/instances/${encodeURIComponent(instanceName)}/snapshots/${encodeURIComponent(snapshotName)}?${qs}`,
      { method: "DELETE" },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    );
  },
  restore: async (
    instanceName: string,
    snapshotName: string,
    body: SnapshotRestoreRequest,
  ): Promise<SnapshotActionResponse> => {
    return fetchAPI<SnapshotActionResponse>(
      `/instances/${encodeURIComponent(instanceName)}/snapshots/${encodeURIComponent(snapshotName)}/restore`,
      { method: "POST", body: JSON.stringify(body) },
      undefined,
      false,
      { retryAfterAuthRefresh: false },
    );
  },
};

// Consolidated API export
export const api = {
  vms: vmAPI,
  images: imageAPI,
  nodes: nodeAPI,
  projects: projectAPI,
  tags: tagAPI,
  system: systemAPI,
  health: healthCheck,
  // New LXD Features
  instances: instanceAPI,
  instanceDevices: instanceDeviceAPI,
  profiles: profileAPI,
  networks: networkAPI,
  networkACLs: networkACLAPI,
  storagePools: storagePoolAPI,
  storageVolumes: storageVolumeAPI,
  isoUploads: isoUploadAPI,
  imageUploads: imageUploadAPI,
  warnings: warningAPI,
  server: serverAPI,
  identities: identityAPI,
  groups: groupAPI,
  clusters: clusterAPI,
  ipam: ipamAPI,
  myKeys: myKeysAPI,
  monitoring: monitoringAPI,
  certificates: certificateAPI,
  integrations: integrationsAPI,
  regions: regionsAPI,
  breakGlass: breakGlassAPI,
  snapshots: snapshotAPI,
  snapshotSchedules: snapshotScheduleAPI,
  clusterMembers: {
    list: async (node: string, opts: ReadOptions = {}) => {
      return fetchRead<ClusterMembersResponse>(`/cluster-members?node=${encodeURIComponent(node)}`, opts);
    },
    get: async (node: string, memberName: string) => {
      return fetchAPI<ClusterMember>(
        `/cluster-members/${encodeURIComponent(memberName)}?node=${encodeURIComponent(node)}`
      );
    },
    getResources: async (node: string, memberName: string, opts: ReadOptions = {}) => {
      return fetchHeavyRead<MemberResources>(
        `/cluster-members/${encodeURIComponent(memberName)}/resources?node=${encodeURIComponent(node)}`,
        opts,
      );
    },
  },

  // ── Organizations ──
  organizations: {
    list: () => fetchAPI<Organization[]>("/organizations"),
    get: (id: string) => fetchAPI<Organization>(`/organizations/${id}`),
    // Whether an agent may see and drive this organization's guest desktops. Four separate
    // decisions rather than one, because they are four separate disclosures: a screenshot, a
    // keystroke, the clipboard, and a shell inside the container.
    desktopControl: {
      get: (orgId: number) =>
        fetchAPI<DesktopControlPolicy>(`/organizations/${orgId}/desktop-control`),
      set: (orgId: number, data: DesktopControlPolicyRequest) =>
        fetchAPI<DesktopControlPolicy>(`/organizations/${orgId}/desktop-control`, {
          method: "PUT",
          body: JSON.stringify(data),
        }),
    },
    create: (data: OrganizationCreateRequest) =>
      fetchAPI<Organization>("/organizations", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    update: (id: string, data: OrganizationUpdateRequest) =>
      fetchAPI<Organization>(`/organizations/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      fetchAPI<{ message: string }>(`/organizations/${id}`, { method: "DELETE" }),
    // members
    listMembers: (orgId: string) =>
      fetchAPI<OrganizationMember[]>(`/organizations/${orgId}/members`),
    addMember: (orgId: string, data: { user_email: string }) =>
      fetchAPI<OrganizationMember>(`/organizations/${orgId}/members`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    updateMember: (orgId: string, email: string, data: Record<string, unknown>) =>
      fetchAPI<OrganizationMember>(
        `/organizations/${orgId}/members/${encodeURIComponent(email)}`,
        { method: "PUT", body: JSON.stringify(data) }
      ),
    removeMember: (orgId: string, email: string) =>
      fetchAPI<{ message: string }>(
        `/organizations/${orgId}/members/${encodeURIComponent(email)}`,
        { method: "DELETE" }
      ),
    // clusters
    listClusters: (orgId: string) =>
      fetchAPI<OrganizationCluster[]>(`/organizations/${orgId}/clusters`),
    bindCluster: (orgId: string, data: { cluster_id: string }) =>
      fetchAPI<OrganizationCluster>(`/organizations/${orgId}/clusters`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    unbindCluster: (orgId: string, clusterId: string) =>
      fetchAPI<{ message: string }>(
        `/organizations/${orgId}/clusters/${encodeURIComponent(clusterId)}`,
        { method: "DELETE" }
      ),
    // org-level personalization
    getPreferences: (orgId: string) =>
      fetchAPI<{ organization_id: number; settings: Record<string, any>; updated_by?: string }>(
        `/organizations/${orgId}/preferences`
      ),
    updatePreferences: (orgId: string, settings: Record<string, any>) =>
      fetchAPI<{ organization_id: number; settings: Record<string, any>; updated_by?: string }>(
        `/organizations/${orgId}/preferences`,
        { method: "PUT", body: JSON.stringify({ settings }) }
      ),
    deletePreferences: (orgId: string) =>
      fetchAPI<{ message: string }>(`/organizations/${orgId}/preferences`, {
        method: "DELETE",
      }),
    getMyOrgPreferences: () =>
      fetchAPI<{ organization_id: number; settings: Record<string, any>; updated_by?: string; updated_at?: string }>(
        `/organizations/my-preferences`
      ),
    getMyOrgPreferencesVersion: () =>
      fetchAPI<{ organization_id: number; updated_at: string | null }>(
        `/organizations/my-preferences/version`
      ),
  },

  // ── User Groups (Admin) ──
  userGroups: {
    list: () => fetchAPI<UserGroupEntry[]>("/user-groups"),
    assign: (data: UserGroupAssignRequest) =>
      fetchAPI<UserGroupAssignResponse>("/user-groups/assign", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    remove: (email: string) =>
      fetchAPI<{ message: string }>(
        `/user-groups/${encodeURIComponent(email)}`,
        { method: "DELETE" }
      ),
    syncStatus: () => fetchAPI<AuthentikSyncStatus>("/user-groups/sync-status"),
  },

  // ── Tenant Access Policies (ACL) ──
  tenantAccess: {
    // policy CRUD
    listPolicies: (organizationId?: number) => {
      const params = organizationId ? `?organization_id=${organizationId}` : "";
      return fetchAPI<PolicyListResponse>(`/tenant-access/policies${params}`);
    },
    getPolicy: (policyId: number) =>
      fetchAPI<AccessPolicy>(`/tenant-access/policies/${policyId}`),
    createPolicy: (data: PolicyCreateRequest) =>
      fetchAPI<AccessPolicy>("/tenant-access/policies", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    updatePolicy: (policyId: number, data: PolicyUpdateRequest) =>
      fetchAPI<AccessPolicy>(`/tenant-access/policies/${policyId}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    deletePolicy: (policyId: number) =>
      fetchAPI<void>(`/tenant-access/policies/${policyId}`, { method: "DELETE" }),

    // assignments
    listAssignments: (organizationId?: number, userEmail?: string) => {
      const params = new URLSearchParams();
      if (organizationId) params.set("organization_id", String(organizationId));
      if (userEmail) params.set("user_email", userEmail);
      const qs = params.toString();
      return fetchAPI<AssignmentListResponse>(`/tenant-access/assignments${qs ? `?${qs}` : ""}`);
    },
    createAssignment: (data: AssignmentCreateRequest) =>
      fetchAPI<PolicyAssignment>("/tenant-access/assignments", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    deleteAssignment: (assignmentId: number) =>
      fetchAPI<void>(`/tenant-access/assignments/${assignmentId}`, { method: "DELETE" }),

    // tenant self-view
    myAccess: () => fetchAPI<MyAccessResponse>("/tenant-access/my-access"),
  },
};
