/**
 * The add-on surface.
 *
 * Lives in its own file rather than in `lib/api.ts`, which is already 2097 lines. It uses the
 * same `fetchAPI` and `fetchRead` as everything else, so timeouts, GET deduplication, the 401
 * recovery path and the `?fresh=true` convention all behave identically here.
 */

import { fetchAPI, fetchRead, type ReadOptions } from "@/lib/api";
import type {
  ExtensionDetail,
  ExtensionInstall,
  ExtensionInstallPreview,
  ExtensionInstallRequest,
  ExtensionInstallResponse,
  ExtensionVocabulary,
  WebhookDelivery,
  WebhookSubscription,
  WebhookSubscriptionRequest,
  WebhookSecretResponse,
} from "@/types/api";

const base = "/extensions";
const install = (id: number | string) => `${base}/installations/${encodeURIComponent(String(id))}`;

export const extensionsAPI = {
  /**
   * Served, never hardcoded.
   *
   * The same rule the capability picker already follows: a client holding its own copy of this
   * list will eventually offer something the server refuses, and the operator sees a form that
   * lies about what it is granting.
   */
  vocabulary: () => fetchAPI<ExtensionVocabulary>(`${base}/vocabulary`),

  list: (opts: ReadOptions = {}) => fetchRead<ExtensionDetail[]>(base, opts),
  get: (slug: string) => fetchAPI<ExtensionDetail>(`${base}/${encodeURIComponent(slug)}`),

  /**
   * What installing would actually grant.
   *
   * The intersection is computed on the server and echoed back. A browser working it out for
   * itself can disagree with the server, and the moment it disagrees is the moment somebody is
   * approving something.
   */
  installPreview: (slug: string, organizationId: number) =>
    fetchAPI<ExtensionInstallPreview>(
      `${base}/${encodeURIComponent(slug)}/install-preview?organization_id=${organizationId}`,
    ),

  install: (slug: string, body: ExtensionInstallRequest) =>
    fetchAPI<ExtensionInstallResponse>(`${base}/${encodeURIComponent(slug)}/installations`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listInstalls: (opts: ReadOptions = {}) =>
    fetchRead<ExtensionInstall[]>(`${base}/installations`, opts),
  getInstall: (id: number) => fetchAPI<ExtensionInstall>(install(id)),

  suspend: (id: number, reason?: string) =>
    fetchAPI<ExtensionInstall>(`${install(id)}/suspend`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  resume: (id: number) => fetchAPI<ExtensionInstall>(`${install(id)}/resume`, { method: "POST" }),

  /** Returns the new credential once. There is no way to read it back afterwards. */
  rotateCredential: (id: number, graceSeconds = 0) =>
    fetchAPI<ExtensionInstallResponse>(`${install(id)}/credential/rotate`, {
      method: "POST",
      body: JSON.stringify({ grace_seconds: graceSeconds }),
    }),

  /**
   * Omitting a secret key leaves its stored value alone, so saving one field cannot blank a
   * credential nobody retyped.
   */
  updateSettings: (id: number, values: Record<string, unknown>) =>
    fetchAPI<ExtensionInstall>(`${install(id)}/settings`, {
      method: "PUT",
      body: JSON.stringify({ values }),
    }),

  uninstall: (id: number) => fetchAPI<void>(install(id), { method: "DELETE" }),

  listSubscriptions: (id: number, opts: ReadOptions = {}) =>
    fetchRead<WebhookSubscription[]>(`${install(id)}/webhooks`, opts),

  /** Returns the signing secret once. Re-reading a subscription never returns it again. */
  createSubscription: (id: number, body: WebhookSubscriptionRequest) =>
    fetchAPI<WebhookSecretResponse>(`${install(id)}/webhooks`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Sends one signed ping. The subscription goes active only if the endpoint answers 2xx. */
  verifySubscription: (id: number, subscriptionId: number) =>
    fetchAPI<WebhookSubscription>(`${install(id)}/webhooks/${subscriptionId}/verify`, {
      method: "POST",
    }),

  rotateSubscriptionSecret: (id: number, subscriptionId: number) =>
    fetchAPI<WebhookSecretResponse>(`${install(id)}/webhooks/${subscriptionId}/rotate-secret`, {
      method: "POST",
    }),

  deleteSubscription: (id: number, subscriptionId: number) =>
    fetchAPI<void>(`${install(id)}/webhooks/${subscriptionId}`, { method: "DELETE" }),

  listDeliveries: (id: number, state?: string, opts: ReadOptions = {}) =>
    fetchRead<WebhookDelivery[]>(
      `${install(id)}/deliveries${state ? `?state=${encodeURIComponent(state)}` : ""}`,
      opts,
    ),

  /** A fresh attempt carrying the same event id, so a receiver's own dedupe still works. */
  redeliver: (id: number, deliveryId: number) =>
    fetchAPI<WebhookDelivery>(`${install(id)}/deliveries/${deliveryId}/redeliver`, {
      method: "POST",
    }),
};
