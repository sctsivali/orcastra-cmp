"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
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
import { formatDateTime } from "@/lib/utils";
import { extensionsAPI } from "@/lib/extensions-api";
import { DELIVERY_STATE, SUBSCRIPTION_STATUS } from "@/lib/extension-display";
import type {
  ExtensionVocabulary,
  WebhookDeliveryState,
  WebhookSecretResponse,
} from "@/types/api";

/**
 * Where an installation's events go, and what happened to each one.
 *
 * note: the delivery log is a feed rather than a `<Table>`. The table contract in this repo
 * requires every visible column to be filterable, and it is right to: a column an operator can
 * read but not narrow is a column that stops being usable at a hundred rows. A delivery log is
 * read newest-first and narrowed by state, so borrowing table markup would claim a contract this
 * surface does not honour, which is the failure the ratchet exists to catch.
 *
 * It is paged all the same. Being a feed was never a reason to stop at the newest fifty, which is
 * what it did: the server capped at fifty with no offset, so the fifty-first delivery was
 * unreachable and nothing said the list had been cut.
 */
const DELIVERY_PAGE_SIZE = 25;
const DELIVERY_STATE_PARAM = "delivery";

/**
 * The states where sending again means something.
 *
 * `delivered` is already done, and `pending` or `inflight` is one the dispatcher still owns, so
 * offering Retry there would race the sender rather than help.
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
  const router = useRouter();
  const searchParams = useSearchParams();
  const [endpoint, setEndpoint] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [page, setPage] = useState(1);

  // The parent already keeps the open panel in the URL so a delivery log can be linked to. A
  // filtered log is what somebody actually wants to send, so the filter goes there too.
  const stateFilter = searchParams.get(DELIVERY_STATE_PARAM) ?? "";
  const setStateFilter = (next: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (next) params.set(DELIVERY_STATE_PARAM, next);
    else params.delete(DELIVERY_STATE_PARAM);
    router.replace(`?${params.toString()}`, { scroll: false });
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
    // loads rather than the list blinking through an empty state on every step.
    placeholderData: (previous) => previous,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["extensions", "subscriptions", installId] });
    queryClient.invalidateQueries({ queryKey: ["extensions", "deliveries", installId] });
  };

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
        title: "Queued again",
        description: "The same event id is reused, so a receiver that dedupes sees one event.",
      });
    },
    onError: (error: Error) =>
      toast({ title: "Could not retry", description: error.message, variant: "destructive" }),
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
      toast({ title: "Could not subscribe", description: error.message, variant: "destructive" }),
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
        ) : (subscriptions.data ?? []).length === 0 ? (
          <EmptyState
            icon={Send}
            title="Nothing is subscribed yet"
            description="Add an endpoint below and this add-on will start receiving events."
          />
        ) : (
          <ul className="divide-y rounded-lg border">
            {(subscriptions.data ?? []).map((row) => {
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
                      {row.event_types.length} event type
                      {row.event_types.length === 1 ? "" : "s"} · secret{" "}
                      <span className="font-mono">{row.secret_fingerprint}</span>
                      {row.consecutive_failures > 0
                        ? ` · ${row.consecutive_failures} consecutive failures`
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
                    {formatDateTime(row.created_at)}
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
 * The boundary the query string requires.
 *
 * The delivery state filter lives in the query string so a filtered log can be linked to, which
 * opts this out of prerendering and makes the boundary a build requirement rather than a nicety.
 */
export function WebhookPanel(props: WebhookPanelProps) {
  return (
    <Suspense fallback={<Skeleton className="h-64 rounded-lg" />}>
      <WebhookPanelContent {...props} />
    </Suspense>
  );
}
