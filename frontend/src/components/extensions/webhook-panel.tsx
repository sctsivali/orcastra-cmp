"use client";

import { Suspense, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { currentQuery, replaceQuery } from "@/lib/url-state";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RotateCcw, Send, Trash2 } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OverflowText } from "@/components/ui/overflow-text";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { formatDateTimeSeconds, plural } from "@/lib/utils";
import { extensionsAPI } from "@/lib/extensions-api";
import { DELIVERY_STATE, SUBSCRIPTION_STATUS } from "@/lib/extension-display";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import type {
  ExtensionVocabulary,
  WebhookDeliveryState,
  WebhookSecretResponse,
} from "@/types/api";

/**
 * Delivery log page size, paged by server offset. The log is a feed rather than a `<Table>`: it
 * is read newest-first and narrowed by state only, while the table contract requires every
 * visible column to be filterable.
 */
const DELIVERY_PAGE_SIZE = 25;
const DELIVERY_STATE_PARAM = "delivery";

/**
 * The states where sending again makes sense. `delivered` is done, and `pending` or `inflight`
 * still belongs to the dispatcher, so a Retry there would race the sender.
 */
const RETRYABLE = new Set<WebhookDeliveryState>(["failed", "dead", "dropped"]);

interface WebhookPanelProps {
  installId: number;
  vocabulary?: ExtensionVocabulary;
  onSecretIssued: (secret: WebhookSecretResponse) => void;
}

function WebhookPanelContent({ installId, vocabulary, onSecretIssued }: WebhookPanelProps) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [endpoint, setEndpoint] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  // `page` is the delivery log's and maps to the server offset. The subscription list is held
  // whole in memory and paged here, so it has its own.
  const [page, setPage] = useState(1);
  const [subscriptionPage, setSubscriptionPage] = useState(1);

  // The parent keeps the open panel in the URL so a delivery log can be linked to; the state
  // filter goes there too.
  const stateFilter = searchParams.get(DELIVERY_STATE_PARAM) ?? "";
  const setStateFilter = (next: string) => {
    const params = currentQuery();
    if (next) params.set(DELIVERY_STATE_PARAM, next);
    else params.delete(DELIVERY_STATE_PARAM);
    replaceQuery(pathname, params);
    setPage(1);
  };

  const subscriptions = useQuery({
    queryKey: ["extensions", "subscriptions", installId],
    queryFn: () => extensionsAPI.listSubscriptions(installId),
  });

  const deliveries = useQuery({
    queryKey: ["extensions", "deliveries", installId, stateFilter, page],
    queryFn: () =>
      extensionsAPI.listDeliveries(installId, {
        state: stateFilter || undefined,
        limit: DELIVERY_PAGE_SIZE,
        offset: (page - 1) * DELIVERY_PAGE_SIZE,
      }),
    // The window shifts under a live log, so the previous page stays on screen while the next one
    // loads instead of blinking through an empty state.
    placeholderData: (previous) => previous,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["extensions", "subscriptions", installId] });
    queryClient.invalidateQueries({ queryKey: ["extensions", "deliveries", installId] });
  };

  const subscriptionRows = subscriptions.data ?? [];
  const subscriptionPages = Math.max(1, Math.ceil(subscriptionRows.length / PANEL_PAGE_SIZE));
  // Clamped on read, like the delivery pager below. Removing the only subscription on the last
  // page shortens the list, and an effect would paint that empty page once first.
  const currentSubscriptionPage = Math.min(subscriptionPage, subscriptionPages);
  const pagedSubscriptions = subscriptionRows.slice(
    (currentSubscriptionPage - 1) * PANEL_PAGE_SIZE,
    currentSubscriptionPage * PANEL_PAGE_SIZE,
  );

  const deliveryRows = deliveries.data?.deliveries ?? [];
  const deliveryTotal = deliveries.data?.total ?? 0;
  const deliveryPages = Math.max(1, Math.ceil(deliveryTotal / DELIVERY_PAGE_SIZE));
  // Clamped on read. The log grows while it is being read, so a page can vanish under the reader.
  const deliveryPage = Math.min(page, deliveryPages);

  const redeliver = useMutation({
    mutationFn: (deliveryId: number) => extensionsAPI.redeliver(installId, deliveryId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["extensions", "deliveries", installId] });
      toast({
        title: "Delivery queued again",
        description: "The same event id is reused, so a receiver that dedupes sees one event.",
      });
    },
    onError: (error: Error) =>
      toast({ title: "Could not retry delivery", description: error.message, variant: "destructive" }),
  });

  const create = useMutation({
    mutationFn: () =>
      extensionsAPI.createSubscription(installId, {
        endpoint_url: endpoint.trim(),
        event_types: selected,
      }),
    onSuccess: (secret) => {
      onSecretIssued(secret);
      setEndpoint("");
      setSelected([]);
      invalidate();
    },
    onError: (error: Error) =>
      toast({ title: "Could not create subscription", description: error.message, variant: "destructive" }),
  });

  const verify = useMutation({
    mutationFn: (subscriptionId: number) =>
      extensionsAPI.verifySubscription(installId, subscriptionId),
    onSuccess: (row) => {
      toast({
        title: row.status === "active" ? "Endpoint verified" : "Endpoint did not answer",
        description:
          row.status === "active"
            ? "Deliveries will start now."
            : row.last_failure_reason ?? "The endpoint did not return a success status.",
        variant: row.status === "active" ? "default" : "destructive",
      });
      invalidate();
    },
  });

  const remove = useMutation({
    mutationFn: (subscriptionId: number) =>
      extensionsAPI.deleteSubscription(installId, subscriptionId),
    onSuccess: invalidate,
  });

  const events = vocabulary?.events ?? [];
  const canSubmit = endpoint.trim().length > 0 && selected.length > 0 && !create.isPending;

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <h3 className="text-lg font-semibold">Subscriptions</h3>

        {subscriptions.isLoading ? (
          <div className="h-24 animate-pulse rounded-lg bg-muted" />
        ) : subscriptionRows.length === 0 ? (
          <EmptyState
            icon={Send}
            title="Nothing is subscribed yet"
            description="Add an endpoint below and this add-on will start receiving events."
          />
        ) : (
          <ul className="divide-y rounded-lg border">
            {pagedSubscriptions.map((row) => {
              const meta = SUBSCRIPTION_STATUS[row.status];
              return (
                <li key={row.id} className="flex flex-wrap items-center gap-3 px-3 py-2">
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <OverflowText
                      value={row.endpoint_url}
                      label={`delivery endpoint for subscription ${row.id}`}
                      className="font-mono text-sm"
                    />
                    <p className="text-xs text-muted-foreground">
                      {plural(row.event_types.length, "event type")} · secret{" "}
                      <span className="font-mono">{row.secret_fingerprint}</span>
                      {row.consecutive_failures > 0
                        ? ` · ${plural(row.consecutive_failures, "consecutive failure")}`
                        : ""}
                    </p>
                  </div>
                  <Badge variant={meta.tone}>{meta.label}</Badge>
                  {row.status === "pending_verification" ? (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => verify.mutate(row.id)}
                      disabled={verify.isPending}
                    >
                      {verify.isPending ? (
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                      ) : (
                        "Verify"
                      )}
                    </Button>
                  ) : null}
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Remove subscription to ${row.endpoint_url}`}
                    onClick={() => remove.mutate(row.id)}
                    disabled={remove.isPending}
                  >
                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </li>
              );
            })}
          </ul>
        )}

        {/* Absent rather than disabled when everything fits. No mt-4 wrapper: the section's
            space-y-3 gap wins over a margin on the child.

            important: keep the label. Two pagers sit on this panel, and without it a screen
            reader hears two identical Previous and Next pairs. */}
        {subscriptionPages > 1 && (
          <DataTablePagination
            variant="compact"
            currentPage={currentSubscriptionPage}
            totalPages={subscriptionPages}
            onPageChange={setSubscriptionPage}
            label={`subscriptions for installation ${installId}`}
          />
        )}

        <div className="space-y-3 rounded-lg border p-3">
          <div className="space-y-1.5">
            <Label htmlFor="webhook-endpoint">Endpoint URL</Label>
            <Input
              id="webhook-endpoint"
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
              autoComplete="off"
            />
            {/* The server owns which addresses are reachable. A second list here would drift from
                it and would leak the network's shape into a JavaScript bundle. */}
            <p className="text-sm text-muted-foreground">
              Must be reachable from this deployment. Private, loopback and carrier-grade-NAT
              addresses are refused, and the refusal names a category rather than an address.
            </p>
          </div>

          <fieldset className="space-y-1.5">
            <legend className="text-sm font-medium">Events</legend>
            <div className="grid gap-1.5 sm:grid-cols-2">
              {events.map((event) => (
                <label key={event.name} className="flex items-start gap-2 text-sm">
                  <Checkbox
                    checked={selected.includes(event.name)}
                    onCheckedChange={(value) =>
                      setSelected((current) =>
                        value === true
                          ? [...current, event.name]
                          : current.filter((n) => n !== event.name),
                      )
                    }
                    className="mt-0.5"
                  />
                  <span className="min-w-0">
                    <span className="block font-mono text-xs">{event.name}</span>
                    <span className="block text-muted-foreground">{event.description}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <Button onClick={() => create.mutate()} disabled={!canSubmit}>
            {create.isPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                Subscribing
              </>
            ) : (
              "Subscribe"
            )}
          </Button>
        </div>
      </section>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-lg font-semibold">Deliveries</h3>
          <div className="flex gap-1">
            {["", "delivered", "failed", "dead", "dropped"].map((state) => (
              <Button
                key={state || "all"}
                size="sm"
                variant={stateFilter === state ? "secondary" : "ghost"}
                onClick={() => setStateFilter(state)}
              >
                {state ? DELIVERY_STATE[state as keyof typeof DELIVERY_STATE].label : "All"}
              </Button>
            ))}
          </div>
        </div>

        {deliveries.isLoading ? (
          <Skeleton className="h-32 rounded-lg" />
        ) : deliveryRows.length === 0 ? (
          <EmptyState
            title={stateFilter ? "No delivery in that state" : "Nothing has been delivered yet"}
            description={
              stateFilter
                ? "Every delivery on this installation is in some other state."
                : "Events appear here as soon as something happens on a cluster this add-on reaches."
            }
          />
        ) : (
          <ul className="divide-y rounded-lg border">
            {deliveryRows.map((row) => {
              const meta = DELIVERY_STATE[row.state];
              return (
                <li key={row.id} className="flex flex-wrap items-center gap-3 px-3 py-2">
                  <Badge variant={meta.tone} className="shrink-0">
                    {meta.label}
                  </Badge>
                  <span className="font-mono text-xs">{row.event_type}</span>
                  <span className="text-xs text-muted-foreground">
                    attempt {row.attempt}
                    {row.response_status ? ` · HTTP ${row.response_status}` : ""}
                    {row.response_ms ? ` · ${row.response_ms}ms` : ""}
                  </span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    {formatDateTimeSeconds(row.created_at)}
                  </span>
                  {RETRYABLE.has(row.state) ? (
                    <Button
                      size="sm"
                      variant="outline"
                      aria-label={`Retry delivery of ${row.event_type}`}
                      disabled={redeliver.isPending}
                      onClick={() => redeliver.mutate(row.id)}
                    >
                      <RotateCcw className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
                      Retry
                    </Button>
                  ) : null}
                  {row.last_error ? (
                    <p className="w-full text-xs text-muted-foreground">{row.last_error}</p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}

        {deliveryPages > 1 && (
          <DataTablePagination
            variant="compact"
            currentPage={deliveryPage}
            totalPages={deliveryPages}
            onPageChange={setPage}
            label={`deliveries for installation ${installId}`}
          />
        )}

        <Alert>
          <AlertDescription className="text-sm text-muted-foreground">
            Delivery is at-least-once, so a receiver will occasionally see the same event twice
            and must dedupe on its event id. Withheld means the installation stopped reaching that
            cluster, so the event was not sent rather than failing.
          </AlertDescription>
        </Alert>
      </section>
    </div>
  );
}

/**
 * Carries its own Suspense boundary. The delivery state filter lives in the query string, which
 * opts this out of prerendering, so `next build` needs a boundary above it.
 */
export function WebhookPanel(props: WebhookPanelProps) {
  return (
    <Suspense fallback={<Skeleton className="h-64 rounded-lg" />}>
      <WebhookPanelContent {...props} />
    </Suspense>
  );
}
