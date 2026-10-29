"use client";

import { useState } from "react";
import { ShieldAlert, Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useBreakGlassStore } from "@/lib/break-glass-store";

const DURATIONS = [
  { value: "15", label: "15 minutes" },
  { value: "30", label: "30 minutes" },
  { value: "60", label: "1 hour" },
  { value: "120", label: "2 hours" },
  { value: "240", label: "4 hours" },
];

/**
 * Shown when a platform admin opens a console/terminal on a tenant cluster they do not
 * own. The admin must justify the access; on submit a time-boxed break-glass grant is
 * created and the original open proceeds. The access is audited and visible to the tenant.
 */
export function BreakGlassModal() {
  const prompt = useBreakGlassStore((s) => s.prompt);
  const submitting = useBreakGlassStore((s) => s.submitting);
  const error = useBreakGlassStore((s) => s.error);
  const submit = useBreakGlassStore((s) => s.submit);
  const cancel = useBreakGlassStore((s) => s.cancel);

  const [reason, setReason] = useState("");
  const [duration, setDuration] = useState("60");
  const [ticket, setTicket] = useState("");

  const open = !!prompt;

  const onSubmit = async () => {
    await submit(reason.trim(), parseInt(duration, 10), ticket.trim() || undefined);
  };

  const handleOpenChange = (next: boolean) => {
    if (!next && !submitting) {
      cancel();
      setReason("");
      setTicket("");
      setDuration("60");
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldAlert className="h-5 w-5 text-status-pending" aria-hidden="true" />
            Break-Glass Access Required
          </DialogTitle>
          <DialogDescription>
            {prompt?.instanceName} is on a cluster you do not own
            {prompt?.targetOrgSlug ? <> ({prompt.targetOrgSlug})</> : null}. Accessing it
            is recorded and visible to the tenant. Provide a justification to proceed.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label htmlFor="bg-reason">Reason <span className="text-destructive">*</span></Label>
            <Textarea
              id="bg-reason"
              placeholder="e.g. investigating ticket INC-1234 on tenant request"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={3}
              autoFocus
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="bg-duration">Access window</Label>
              <Select value={duration} onValueChange={setDuration}>
                <SelectTrigger id="bg-duration"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {DURATIONS.map((d) => (
                    <SelectItem key={d.value} value={d.value}>{d.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="bg-ticket">Ticket (optional)</Label>
              <Input id="bg-ticket" placeholder="INC-1234" value={ticket} onChange={(e) => setTicket(e.target.value)} />
            </div>
          </div>
          {error ? (
            <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={onSubmit} disabled={submitting || reason.trim().length === 0}>
            {submitting ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Granting...</>) : "Grant & open"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
