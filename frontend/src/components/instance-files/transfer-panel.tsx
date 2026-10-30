"use client";

import * as React from "react";
import { CheckCircle2, ChevronDown, ChevronUp, FolderUp, RotateCcw, X, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import { Progress } from "@/components/ui/progress";
import { guestText } from "@/lib/instance-files/guest-text";
import type { GuestTarget } from "@/lib/instance-files/api";
import { cancelAll, cancelTransfer, discardInterrupted, replaceConflicts, resumeWithFile, skipConflicts, retryTransfer } from "@/lib/instance-files/transfer-pump";
import { isSameTarget, isActive, useTransferStore, type Transfer } from "@/lib/instance-files/transfer-store";
import { formatBytes, formatNetworkRate } from "@/lib/utils";

function etaText(remaining: number, rate: number): string {
  if (rate <= 0) return "";
  const seconds = Math.ceil(remaining / rate);
  if (seconds < 60) return `${seconds} s left`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} min left`;
  return `${(seconds / 3600).toFixed(1)} h left`;
}

export function summarizeTransfers(transfers: Transfer[]) {
  const active = transfers.filter(isActive);
  const size = active.reduce((sum, item) => sum + item.size, 0);
  const sent = active.reduce((sum, item) => sum + item.sent, 0);
  const rate = active.reduce((sum, item) => sum + (item.state === "uploading" ? item.rate : 0), 0);
  return { active, size, sent, rate, percent: size ? Math.round((sent / size) * 100) : 0 };
}

function TransferRow({ transfer }: { transfer: Transfer }) {
  const inputRef = React.useRef<HTMLInputElement>(null);
  const [mismatch, setMismatch] = React.useState(false);
  const percent = transfer.size ? Math.round((transfer.sent / transfer.size) * 100) : 100;
  const name = guestText(transfer.name);
  return (
    <li className="flex items-center gap-2 px-2 py-1 text-xs">
      {transfer.state === "done" ? (
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" aria-hidden="true" />
      ) : transfer.state === "failed" || transfer.state === "conflict" ? (
        <XCircle className="h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden="true" />
      ) : null}
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-baseline justify-between gap-2">
          <OverflowText label="File name" value={name} mono mode="overlay" className="min-w-0" />
          <span className="shrink-0 tabular-nums text-muted-foreground">
            {transfer.state === "uploading" && `${percent}%`}
            {transfer.state === "queued" && "Waiting"}
            {transfer.state === "finishing" && "Finishing"}
            {transfer.state === "done" && formatBytes(transfer.size)}
            {transfer.state === "cancelled" && "Cancelled"}
          </span>
        </div>
        {transfer.state === "uploading" && (
          <Progress
            value={percent}
            className="mt-1 h-1 motion-reduce:transition-none"
            aria-label={`Uploading ${name}`}
            aria-valuetext={`${percent} percent`}
          />
        )}
        {transfer.error && <p className="text-destructive">{transfer.error}</p>}
        {transfer.state === "interrupted" && (
          <p className="text-muted-foreground">
            {mismatch ? "That is not the same file. " : ""}This upload stopped when the page reloaded.
          </p>
        )}
      </div>
      {transfer.state === "conflict" && (
        <>
          <Button variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={() => retryTransfer(transfer.id, "replace")}>Replace</Button>
          <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => cancelTransfer(transfer.id)}>Skip</Button>
        </>
      )}
      {transfer.state === "failed" && (
        <Button variant="ghost" size="icon" className="h-6 w-6" aria-label={`Retry ${name}`} onClick={() => retryTransfer(transfer.id)}>
          <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
        </Button>
      )}
      {transfer.state === "interrupted" && (
        <>
          <input
            ref={inputRef}
            type="file"
            hidden
            aria-label={`Choose ${name} again`}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) setMismatch(!resumeWithFile(transfer.id, file));
              event.target.value = "";
            }}
          />
          <Button variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={() => inputRef.current?.click()}>Choose file</Button>
          <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => discardInterrupted(transfer.id)}>Discard</Button>
        </>
      )}
      {(transfer.state === "queued" || transfer.state === "uploading" || transfer.state === "failed") && (
        <Button variant="ghost" size="icon" className="h-6 w-6" aria-label={`Cancel ${name}`} onClick={() => cancelTransfer(transfer.id)}>
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </Button>
      )}
    </li>
  );
}

/** The queue, for the instance this explorer shows. */
export function TransferPanel({ target }: { target: GuestTarget }) {
  const all = useTransferStore((state) => state.transfers);
  const remove = useTransferStore((state) => state.remove);
  const [expanded, setExpanded] = React.useState(false);
  const transfers = React.useMemo(
    () =>
      all.filter((item) => isSameTarget(item.target, target)),
    [all, target],
  );
  const { active, sent, size, rate, percent } = summarizeTransfers(transfers);
  const needsYou = transfers.filter((item) => ["failed", "conflict", "interrupted"].includes(item.state));
  const conflicts = transfers.filter((item) => item.state === "conflict").length;
  if (transfers.length === 0) return null;
  const headline = active.length
    ? `Uploading ${active.length} of ${transfers.length}`
    : needsYou.length
      ? `${needsYou.length} need attention`
      : `${transfers.length} uploaded`;
  const detail = active.length
    ? [rate > 0 ? formatNetworkRate(rate) : "", etaText(size - sent, rate)].filter(Boolean).join(", ")
    : "";
  return (
    <section aria-label="File transfers" className="border-t bg-muted/20">
      <div className="flex min-h-[32px] items-center gap-2 px-2 text-xs">
        <FolderUp className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="shrink-0 font-medium">{headline}</span>
        {detail && <span className="shrink-0 whitespace-nowrap tabular-nums text-muted-foreground">{detail}</span>}
        {active.length > 0 && (
          <Progress value={percent} className="h-1 min-w-[3rem] flex-1 motion-reduce:transition-none" aria-label="Upload progress" aria-valuetext={`${percent} percent`} />
        )}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          {active.length > 0 ? (
            <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => cancelAll(target)}>Cancel all</Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => remove(transfers.filter((item) => item.state === "done" || item.state === "cancelled").map((item) => item.id))}
            >
              Clear
            </Button>
          )}
          {conflicts > 1 && (
            <>
              <Button variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={() => replaceConflicts(target)}>Replace all</Button>
              <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => skipConflicts(target)}>Skip all</Button>
            </>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            aria-label={expanded ? "Hide the transfer list" : "Show the transfer list"}
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />}
          </Button>
        </div>
      </div>
      {(expanded || needsYou.length > 0) && (
        <ul className="max-h-40 overflow-auto border-t">
          {(expanded ? transfers : needsYou).slice(0, 200).map((transfer) => (
            <TransferRow key={transfer.id} transfer={transfer} />
          ))}
        </ul>
      )}
    </section>
  );
}
