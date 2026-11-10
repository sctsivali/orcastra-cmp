"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, PauseCircle } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { OverflowText } from "@/components/ui/overflow-text";
import { INSTALL_STATUS } from "@/lib/extension-display";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { formatDateTimeSeconds, plural } from "@/lib/utils";
import type {
  ExtensionInstall,
  WebhookDeliveryState,
  WebhookSubscription,
} from "@/types/api";

interface AddonHealthPanelProps {
  install: ExtensionInstall;
  subscriptions: WebhookSubscription[];
  /**
   * The server's breakdown over every delivery on this installation. Counts are used because the
   * delivery rows the page holds are one capped page and would undercount past it.
   */
  deliveryCounts: Partial<Record<WebhookDeliveryState, number>>;
  deliveryTotal: number;
}

/** Shown for a value that could not be read, which is never rendered as zero. */
const UNKNOWN = "Not recorded";

function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return Math.floor((at - Date.now()) / 86_400_000);
}

/**
 * Whether this add-on is working: credential use and expiry, failing webhook endpoints and
 * delivery outcomes, all read from the server response. An unknown is shown as unknown rather than
 * zero, because "never used" and "could not read the key" call for opposite actions.
 */
export function AddonHealthPanel({
  install,
  subscriptions,
  deliveryCounts,
  deliveryTotal,
}: AddonHealthPanelProps) {
  const readable = install.credential_readable !== false;
  const expiresInDays = daysUntil(install.credential_expires_at);
  const failing = useMemo(
    () => subscriptions.filter((s) => (s.consecutive_failures ?? 0) > 0),
    [subscriptions],
  );
  const suspendedSubscriptions = subscriptions.filter((s) => s.status === "suspended");

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  // Clamped on read rather than corrected in an effect. An endpoint recovering shortens this list
  // while it is open, and an effect would paint one wrong frame first.
  const totalPages = Math.max(1, Math.ceil(failing.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedFailing = failing.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const dead = deliveryCounts.dead ?? 0;
  const dropped = deliveryCounts.dropped ?? 0;

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">Health</h2>

      {install.status === "suspended" && (
        <Alert>
          <PauseCircle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            Suspended {install.suspended_at ? formatDateTimeSeconds(install.suspended_at) : ""}
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
        {/* Status is a badge rather than a control; the suspension notice above explains it. */}
        <div>
          <dt className="text-sm text-muted-foreground">Status</dt>
          <dd className="text-sm">
            <Badge variant={INSTALL_STATUS[install.status].tone}>
              {INSTALL_STATUS[install.status].label}
            </Badge>
          </dd>
        </div>
        <div>
          <dt className="text-sm text-muted-foreground">Credential last used</dt>
          <dd className="text-sm">
            {readable && install.credential_last_used_at
              ? formatDateTimeSeconds(install.credential_last_used_at)
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
                <span>{formatDateTimeSeconds(install.credential_expires_at)}</span>
                {expiresInDays !== null && expiresInDays <= 14 && (
                  <Badge variant={expiresInDays <= 0 ? "destructive" : "caution"}>
                    {expiresInDays <= 0 ? "Expired" : `${plural(expiresInDays, "day")} left`}
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
        <div>
          <dt className="text-sm text-muted-foreground">Deliveries</dt>
          <dd className="text-sm">
            {deliveryTotal === 0
              ? "None yet"
              : `${deliveryTotal} recorded, ${deliveryCounts.delivered ?? 0} delivered`}
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
            {dead > 0 && `${plural(dead, "delivery", "deliveries")} gave up after every retry. `}
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
            {plural(suspendedSubscriptions.length, "endpoint has", "endpoints have")} been suspended
            after repeated failures. Fix the endpoint, then verify it again to resume delivery.
          </AlertDescription>
        </Alert>
      )}

      {failing.length > 0 && (
        <ul className="space-y-2">
          {pagedFailing.map((subscription) => (
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
                {subscription.last_failure_at ? formatDateTimeSeconds(subscription.last_failure_at) : UNKNOWN}
                {subscription.last_failure_reason ? `: ${subscription.last_failure_reason}` : ""}
              </p>
              <p className="text-xs text-muted-foreground">
                Last success{" "}
                {subscription.last_success_at
                  ? formatDateTimeSeconds(subscription.last_success_at)
                  : "never"}
              </p>
            </li>
          ))}
        </ul>
      )}

      {/* Absent rather than disabled when every failing endpoint fits on one page. The section
          already spaces its children, so this carries no margin of its own. */}
      {totalPages > 1 && (
        <DataTablePagination
          currentPage={currentPage}
          totalPages={totalPages}
          pageSize={pageSize}
          pageSizeOptions={[...PANEL_PAGE_SIZES]}
          totalItems={failing.length}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      )}
    </section>
  );
}
