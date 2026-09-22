/**
 * Break-Glass store - drives the pre-flight gate for opening a console/terminal.
 *
 * Before a console/terminal opens, the console/terminal stores call `request(clusterId)`.
 * If the backend says a grant is required and the actor has none, a modal is shown; the
 * promise resolves true once a grant is created, or false if the user cancels. Owned/
 * in-scope access (and any check failure) resolves true immediately - the server-side
 * gate (HTTP 403 / WS close 1008) remains the authoritative backstop.
 */
import { create } from 'zustand';
import { api, onAuthActorChanged, type BreakGlassCheck } from '@/lib/api';
import {
  captureExternalMutationContext,
  reconcileExternalMutation,
  type ExternalMutationContext,
} from '@/lib/mutation-coherence';

interface BreakGlassPrompt {
  clusterId: string;
  instanceName: string;
  targetOrgSlug: string | null;
  actorEpoch: number;
  mutationContext: ExternalMutationContext | null;
  resolve: (allowed: boolean) => void;
}

interface BreakGlassState {
  prompt: BreakGlassPrompt | null;
  submitting: boolean;
  error: string | null;
  request: (clusterId: string, instanceName: string) => Promise<boolean>;
  submit: (reason: string, durationMinutes: number, ticketRef?: string) => Promise<void>;
  cancel: () => void;
}

let actorEpoch = 0;
let requestController: AbortController | null = null;
let submitController: AbortController | null = null;

export const useBreakGlassStore = create<BreakGlassState>((set, get) => ({
  prompt: null,
  submitting: false,
  error: null,

  request: async (clusterId, instanceName) => {
    const requestEpoch = actorEpoch;
    requestController?.abort();
    submitController?.abort();
    submitController = null;
    const previousPrompt = get().prompt;
    previousPrompt?.resolve(false);
    if (previousPrompt) set({ prompt: null, submitting: false, error: null });
    const controller = new AbortController();
    requestController = controller;
    let check: BreakGlassCheck;
    try {
      check = await api.breakGlass.check(clusterId, { signal: controller.signal });
    } catch {
      if (controller.signal.aborted || requestEpoch !== actorEpoch) return false;
      // fail-open: a transient check failure must not block legitimate access; the
      // backend gate still enforces and would close the connection if truly disallowed.
      return true;
    } finally {
      if (requestController === controller) requestController = null;
    }
    if (controller.signal.aborted || requestEpoch !== actorEpoch) return false;
    if (!check.required || check.has_active_grant) return true;
    return new Promise<boolean>((resolve) => {
      set({
        prompt: {
          clusterId,
          instanceName,
          targetOrgSlug: check.target_organization_slug,
          actorEpoch: requestEpoch,
          mutationContext: captureExternalMutationContext(),
          resolve,
        },
        error: null,
        submitting: false,
      });
    });
  },

  submit: async (reason, durationMinutes, ticketRef) => {
    const p = get().prompt;
    if (!p) return;
    if (p.actorEpoch !== actorEpoch) {
      p.resolve(false);
      set({ prompt: null, submitting: false, error: null });
      return;
    }
    // important: the modal's button reads `submitting` through a subscription, so it only goes
    // disabled on the next render. Two clicks inside one tick would both create a grant, and a
    // break-glass grant is an audited privilege escalation. zustand's set is synchronous, so
    // re-reading it here is a real guard and needs no ref.
    if (get().submitting) return;
    set({ submitting: true, error: null });
    const submitEpoch = actorEpoch;
    const controller = new AbortController();
    submitController = controller;
    try {
      await api.breakGlass.createGrant({
        cluster_id: p.clusterId,
        reason,
        duration_minutes: durationMinutes,
        ticket_ref: ticketRef || undefined,
      }, { signal: controller.signal });
      if (
        controller.signal.aborted ||
        submitEpoch !== actorEpoch ||
        get().prompt !== p
      ) return;
      // This store lives outside React Query's hook tree. The provider bridge points at the
      // current actor's QueryClient and is replaced at every auth boundary.
      await reconcileExternalMutation("break-glass.write", {}, p.mutationContext).catch(() => {
        // The grant is already authoritative. The governance panel's bounded poll remains the
        // recovery path if an active refetch fails, so never report a successful grant as failed.
      });
      if (
        controller.signal.aborted ||
        submitEpoch !== actorEpoch ||
        get().prompt !== p
      ) return;
      p.resolve(true);
      set({ prompt: null, submitting: false });
    } catch (e: unknown) {
      if (controller.signal.aborted || submitEpoch !== actorEpoch || get().prompt !== p) return;
      set({ submitting: false, error: e instanceof Error ? e.message : 'Failed to create grant' });
    } finally {
      if (submitController === controller) submitController = null;
    }
  },

  cancel: () => {
    const p = get().prompt;
    if (p) p.resolve(false);
    set({ prompt: null, submitting: false, error: null });
  },
}));

// Actor changes are synchronous auth boundaries. Abort transport, resolve every pending caller
// closed, and erase target metadata before the next actor's descendant tree can mount.
onAuthActorChanged(() => {
  actorEpoch += 1;
  requestController?.abort();
  submitController?.abort();
  requestController = null;
  submitController = null;
  const state = useBreakGlassStore.getState();
  state.prompt?.resolve(false);
  useBreakGlassStore.setState({ prompt: null, submitting: false, error: null });
});
