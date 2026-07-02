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
import { api, type BreakGlassCheck } from '@/lib/api';

interface BreakGlassPrompt {
  clusterId: string;
  instanceName: string;
  targetOrgSlug: string | null;
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

export const useBreakGlassStore = create<BreakGlassState>((set, get) => ({
  prompt: null,
  submitting: false,
  error: null,

  request: async (clusterId, instanceName) => {
    let check: BreakGlassCheck;
    try {
      check = await api.breakGlass.check(clusterId);
    } catch {
      // fail-open: a transient check failure must not block legitimate access; the
      // backend gate still enforces and would close the connection if truly disallowed.
      return true;
    }
    if (!check.required || check.has_active_grant) return true;
    return new Promise<boolean>((resolve) => {
      set({
        prompt: { clusterId, instanceName, targetOrgSlug: check.target_organization_slug, resolve },
        error: null,
        submitting: false,
      });
    });
  },

  submit: async (reason, durationMinutes, ticketRef) => {
    const p = get().prompt;
    if (!p) return;
    set({ submitting: true, error: null });
    try {
      await api.breakGlass.createGrant({
        cluster_id: p.clusterId,
        reason,
        duration_minutes: durationMinutes,
        ticket_ref: ticketRef || undefined,
      });
      p.resolve(true);
      set({ prompt: null, submitting: false });
    } catch (e: unknown) {
      set({ submitting: false, error: e instanceof Error ? e.message : 'Failed to create grant' });
    }
  },

  cancel: () => {
    const p = get().prompt;
    if (p) p.resolve(false);
    set({ prompt: null, submitting: false, error: null });
  },
}));
