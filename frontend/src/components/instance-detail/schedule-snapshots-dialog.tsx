"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { useCreateSnapshotSchedule, useUpdateSnapshotSchedule } from "@/hooks/use-lxd";
import {
  FREQUENCIES,
  MINUTES,
  RETENTIONS,
  WEEKDAYS,
  hourOptions,
  retentionFits,
  scheduleSentence,
} from "@/app/inventory/snapshots/components/snapshot-schedule-vocabulary";
import type { ScheduleFrequency, SnapshotSchedule, VMInfo } from "@/types/api";

const MAX_EXPIRY_DAYS = 7;
const MAX_LABEL = 42;

interface ScheduleDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instance: VMInfo;
  /** Present means edit rather than create. */
  existing?: SnapshotSchedule | null;
}

/**
 * Ask for a schedule as a sentence: snapshot this FOR [label], EVERY [interval] at [time],
 * KEEPING the last [n], EXPIRING each after [d] days.
 *
 * Presets rather than a cron field, because a Select cannot produce an invalid value and nobody
 * has to be taught a syntax. Every preset label begins with a digit, which `check-heading-case`
 * requires of a `label:` literal it is not going to Title Case, and which happens to be the more
 * scannable spelling.
 *
 * Two things are deliberately NOT here. There is no cluster picker: the host and project come
 * from the instance this dialog was opened on, so there is nothing to choose and nothing to get
 * wrong. And there is no acknowledgement block, because there is no acknowledgement that opens
 * this door: an instance the metered product already schedules is refused outright, and the
 * button that opens this dialog is disabled with the reason written beside it.
 */
export function ScheduleSnapshotsDialog({
  open,
  onOpenChange,
  instance,
  existing = null,
}: ScheduleDialogProps) {
  const { toast } = useToast();
  const create = useCreateSnapshotSchedule();
  const update = useUpdateSnapshotSchedule();

  const [label, setLabel] = useState("");
  const [frequency, setFrequency] = useState<ScheduleFrequency>("daily");
  const [hour, setHour] = useState("3");
  const [minute, setMinute] = useState("0");
  const [weekday, setWeekday] = useState("0");
  const [keepLast, setKeepLast] = useState("3");
  const [expiryDays, setExpiryDays] = useState(String(MAX_EXPIRY_DAYS));
  const [error, setError] = useState<string | null>(null);
  const submitInFlightRef = useRef(false);

  // Reset on open, keyed to `open` rather than to the target, so a dialog reopened on a different
  // instance never shows the previous one's draft.
  useEffect(() => {
    if (!open) return;
    setLabel(existing?.label ?? "");
    setFrequency(existing?.frequency ?? "daily");
    setHour(String(existing?.hour_utc ?? 3));
    setMinute(String(existing?.minute_utc ?? 0));
    setWeekday(String(existing?.weekday ?? 0));
    setKeepLast(String(existing?.keep_last ?? 3));
    setExpiryDays(String(existing?.expiry_days ?? MAX_EXPIRY_DAYS));
    setError(null);
    submitInFlightRef.current = false;
  }, [open, existing]);

  const days = Number(expiryDays);
  const expiryValid = Number.isInteger(days) && days >= 1 && days <= MAX_EXPIRY_DAYS;
  const labelValid = label.trim().length > 0 && label.trim().length <= MAX_LABEL;
  const pending = create.isPending || update.isPending;
  // Refused here rather than at the server. The backend answers 422 for this pair, and a 422 an
  // operator has to interpret is worse than a button that is already off with the reason written
  // under it.
  const coherent = !expiryValid || retentionFits(frequency, Number(keepLast), days);
  const canSubmit = labelValid && expiryValid && coherent && !pending;

  async function handleCreate(): Promise<void> {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    try {
      const body = {
        label: label.trim(),
        frequency,
        hour_utc: Number(hour),
        minute_utc: Number(minute),
        weekday: frequency === "weekly" ? Number(weekday) : null,
        keep_last: Number(keepLast),
        expiry_days: days,
      };
      const result = existing
        ? await update.mutateAsync({ id: existing.id, body })
        : await create.mutateAsync({
            ...body,
            host: instance.host,
            project: instance.project || "default",
            instance_name: instance.name,
          });
      toast({ title: existing ? "Schedule updated" : "Schedule created", description: result.message });
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      submitInFlightRef.current = false;
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] flex flex-col overflow-hidden sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{existing ? "Edit This Schedule" : "Schedule Snapshots"}</DialogTitle>
          <DialogDescription>
            Orcastra takes these itself. They are named like the ones you take by hand and expire
            on their own, so nothing accumulates if this console goes away.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="schedule-label">What is this for</Label>
            <Input
              id="schedule-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="nightly"
              maxLength={MAX_LABEL}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              Becomes part of every snapshot name, which starts with{" "}
              <span className="font-mono">ops-</span> and ends with the time it was taken.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="schedule-frequency">Repeat every</Label>
              <Select
                value={frequency}
                onValueChange={(v) => setFrequency(v as ScheduleFrequency)}
              >
                <SelectTrigger id="schedule-frequency">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {FREQUENCIES.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="schedule-keep">Keep the last</Label>
              <Select value={keepLast} onValueChange={setKeepLast}>
                <SelectTrigger id="schedule-keep">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {RETENTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {frequency === "weekly" && (
            <div className="space-y-2">
              <Label htmlFor="schedule-weekday">On</Label>
              <Select value={weekday} onValueChange={setWeekday}>
                <SelectTrigger id="schedule-weekday">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WEEKDAYS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="schedule-hour">At (hour)</Label>
              <Select value={hour} onValueChange={setHour}>
                <SelectTrigger id="schedule-hour">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {hourOptions().map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="schedule-minute">Minute</Label>
              <Select value={minute} onValueChange={setMinute}>
                <SelectTrigger id="schedule-minute">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MINUTES.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="schedule-expiry">Keep for (days)</Label>
            <Input
              id="schedule-expiry"
              type="number"
              min={1}
              max={MAX_EXPIRY_DAYS}
              value={expiryDays}
              onChange={(e) => setExpiryDays(e.target.value)}
              aria-invalid={!expiryValid}
            />
            {!expiryValid && (
              <p className="text-xs text-destructive">
                Choose between 1 and {MAX_EXPIRY_DAYS} days.
              </p>
            )}
          </div>

          {/* Retention and expiry interact and the interaction is invisible. The form does the
              arithmetic once so the operator does not do it wrong every time. */}
          {expiryValid && (
            <p className={coherent ? "text-xs text-muted-foreground" : "text-xs text-destructive"}>
              {scheduleSentence(frequency, Number(keepLast), days)}
            </p>
          )}

        </DialogBody>

        <DialogSubmitError message={error} />

        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancel
          </Button>
          <Button onClick={handleCreate} disabled={!canSubmit}>
            {pending ? "Saving" : existing ? "Save schedule" : "Create schedule"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
