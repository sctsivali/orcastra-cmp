"use client";

import { AlertTriangle, PauseCircle } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { OverflowText } from "@/components/ui/overflow-text";
import { formatDateTime } from "@/lib/utils";
import type { ExtensionInstall, WebhookDelivery, WebhookSubscription } from "@/types/api";

interface AddonHealthPanelProps {
  install: ExtensionInstall;
  subscriptions: WebhookSubscription[];
  deliveries: WebhookDelivery[];
}

/** Nothing to show is not the same as a zero, so it is never rendered as one. */
const UNKNOWN = "Not recorded";

function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return Math.floor((at - Date.now()) / 86_400_000);
}

/**
 * Whether this add-on is actually working, which the page could not answer before.
 *
 * Suspend was offered as an emergency stop with no signal telling an operator when to press it:
 * the page showed capabilities, clusters and a credential id, and nothing about whether the
 * credential was being used, whether deliveries were failing, or whether it was near expiry. All
 * of it was already recorded and none of it was rendered.
 *
 * Everything here is read from what the server already returns. Nothing is inferred, and an
 * unknown is shown as unknown rather than as a zero, because "never used" and "we could not read
 * the key" lead an operator to opposite conclusions.
 */
export function AddonHealthPanel({ install, subscriptions, deliveries }: AddonHealthPanelProps) {
  const readable = install.credential_readable !== false;
  const expiresInDays = daysUntil(install.credential_expires_at);
  const failing = subscriptions.filter((s) => (s.consecutive_failures ?? 0) > 0);
  const suspendedSubscriptions = subscriptions.filter((s) => s.status === "suspended");

  const byState = deliveries.reduce<Record<string, number>>((acc, row) => {
    acc[row.state] = (acc[row.state] ?? 0) + 1;
    return acc;
  }, {});
  const dead = byState.dead ?? 0;
  const dropped = byState.dropped ?? 0;

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">Health</h2>

      {install.status === "suspended" && (
        <Alert>
          <PauseCircle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            Suspended {install.suspended_at ? formatDateTime(install.suspended_at) : ""}
            {install.suspended_by ? ` by ${install.suspended_by}` : ""}.{" "}
            {install.suspend_reason || "No reason was recorded."} The credential is refused while
            this lasts.
          </AlertDescription>
        </Alert>
      )}

      {!readable && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            The credential store could not be read, so the two values below are unknown rather than
            zero. The installation itself is unaffected.
          </AlertDescription>
        </Alert>
      )}

      <dl className="grid gap-x-6 gap-y-3 rounded-lg border p-4 sm:grid-cols-2">
        <div>
          <dt className="text-sm text-muted-foreground">Credential last used</dt>
          <dd className="text-sm">
            {readable && install.credential_last_used_at
              ? formatDateTime(install.credential_last_used_at)
              : readable
                ? "Never. The add-on has not authenticated yet."
                : UNKNOWN}
          </dd>
        </div>
        <div>
          <dt className="text-sm text-muted-foreground">Credential expires</dt>
          <dd className="flex items-center gap-2 text-sm">
            {readable && install.credential_expires_at ? (
              <>
                <span>{formatDateTime(install.credential_expires_at)}</span>
                {expiresInDays !== null && expiresInDays <= 14 && (
                  <Badge variant={expiresInDays <= 0 ? "destructive" : "caution"}>
                    {expiresInDays <= 0 ? "Expired" : `${expiresInDays} days left`}
                  </Badge>
                )}
              </>
            ) : (
              <span>{readable ? "No expiry recorded" : UNKNOWN}</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-sm text-muted-foreground">Version agreed to</dt>
          <dd className="flex items-center gap-2 text-sm">
            <span className="font-mono">{install.extension_version}</span>
            {install.available_version && (
              <Badge variant="caution">{install.available_version} available</Badge>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-sm text-muted-foreground">Webhook subscriptions</dt>
          <dd className="text-sm">
            {subscriptions.length === 0
              ? "None. This add-on polls rather than being told."
              : `${subscriptions.length} total, ${failing.length} currently failing`}
          </dd>
        </div>
      </dl>

      {install.available_version && (
        <p className="text-sm text-muted-foreground">
          This installation keeps the reach it was granted and stays on{" "}
          <span className="font-mono">{install.extension_version}</span>. Moving to{" "}
          <span className="font-mono">{install.available_version}</span> means approving what the
          new version asks for, because it may ask for more.
        </p>
      )}

      {(dead > 0 || dropped > 0) && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            {dead > 0 && `${dead} ${dead === 1 ? "delivery" : "deliveries"} gave up after every retry. `}
            {dropped > 0 &&
              `${dropped} ${dropped === 1 ? "was" : "were"} dropped because this installation no longer reached the cluster the event was about. `}
            Dropped is not a failure to deliver, it is a refusal to.
          </AlertDescription>
        </Alert>
      )}

      {suspendedSubscriptions.length > 0 && (
        <Alert>
          <PauseCircle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            {suspendedSubscriptions.length}{" "}
            {suspendedSubscriptions.length === 1 ? "endpoint has" : "endpoints have"} been suspended
            after repeated failures. Fix the endpoint, then verify it again to resume delivery.
          </AlertDescription>
        </Alert>
      )}

      {failing.length > 0 && (
        <ul className="space-y-2">
          {failing.map((subscription) => (
            <li key={subscription.id} className="rounded-md border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <OverflowText
                  value={subscription.endpoint_url}
                  label={`Endpoint ${subscription.endpoint_url}`}
                  className="font-mono text-xs"
                />
                <Badge variant="destructive">
                  {subscription.consecutive_failures} in a row
                </Badge>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                Last failure{" "}
                {subscription.last_failure_at ? formatDateTime(subscription.last_failure_at) : UNKNOWN}
                {subscription.last_failure_reason ? `: ${subscription.last_failure_reason}` : ""}
              </p>
              <p className="text-xs text-muted-foreground">
                Last success{" "}
                {subscription.last_success_at
                  ? formatDateTime(subscription.last_success_at)
                  : "never"}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
