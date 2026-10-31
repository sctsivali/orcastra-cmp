"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ListChecks, XCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { ApiKeyCapabilitySelector } from "@/components/integrations/api-key-capability-selector";
import { keyOwnsOrganizationsDetail, provisioningWouldStopDetail } from "@/lib/key-ownership-display";
import type { APIKeyInfo, CapabilityOption, ProvisioningWouldStopDetail } from "@/types";

interface EditAPIKeyCapabilitiesDialogProps {
  apiKey: APIKeyInfo;
  capabilities: CapabilityOption[];
  /** Resolves when the edit lands and rejects with the server's error when it does not. */
  onSubmit: (keyId: string, capabilities: string[], force: boolean) => Promise<unknown>;
  isSubmitting: boolean;
}

/**
 * What a key may do now. A legacy key carries no list and holds every capability that is inherited,
 * which leaves out the ones that reach inside a guest.
 */
function seedFrom(apiKey: APIKeyInfo, vocabulary: CapabilityOption[]): string[] {
  if (apiKey.capabilities == null) {
    return vocabulary.filter((option) => option && !option.never_inherited).map((option) => option.name);
  }
  return [...apiKey.capabilities];
}

/**
 * Narrow or widen what an existing key may do, without reissuing it.
 *
 * Withdrawing tenant.provision from a key that owns OrcaHub organizations is asked about: the server
 * answers with what would stop, the dialog shows it, and Withdraw Anyway sends the same edit with
 * force. Withdrawing org.sync or inventory.read from such a key is refused outright, and the dialog
 * shows the server's sentence for that too.
 */
export function EditAPIKeyCapabilitiesDialog({
  apiKey,
  capabilities,
  onSubmit,
  isSubmitting,
}: EditAPIKeyCapabilitiesDialogProps) {
  const [open, setOpen] = useState(false);
  const seed = useMemo(() => seedFrom(apiKey, capabilities), [apiKey, capabilities]);
  const [selected, setSelected] = useState<string[]>(seed);
  const [warning, setWarning] = useState<ProvisioningWouldStopDetail | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const submitInFlightRef = useRef(false);

  // Re-seed each time it opens, so a stale form never writes the old set back.
  useEffect(() => {
    if (!open) return;
    setSelected(seed);
    setWarning(null);
    setRefusal(null);
  }, [open, seed]);

  const toggle = (name: string) => {
    // A changed selection is a different question, so an earlier answer no longer applies.
    setWarning(null);
    setRefusal(null);
    setSelected((prev) => (prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]));
  };

  const save = async (force: boolean) => {
    if (submitInFlightRef.current) return;
    if (selected.length === 0) return;
    submitInFlightRef.current = true;
    try {
      await onSubmit(apiKey.id, selected, force);
    } catch (error) {
      const ask = provisioningWouldStopDetail(error);
      if (ask) {
        setWarning(ask);
        return;
      }
      const owned = keyOwnsOrganizationsDetail(error);
      if (owned) setRefusal(owned.message);
      // Anything else is surfaced by the parent, and the dialog stays open with the selection.
      return;
    } finally {
      submitInFlightRef.current = false;
    }
    setOpen(false);
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    await save(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <ListChecks className="mr-1 h-4 w-4" aria-hidden="true" />
          Edit Capabilities
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>Edit Capabilities</DialogTitle>
          <DialogDescription>
            Change what {String(apiKey.name || "this key")} may do. The credential itself does not
            change, so the integration keeps working.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
          <DialogBody className="-mr-2 pr-2">
            <div className="grid gap-4 py-4">
              {apiKey.capabilities == null && (
                <p role="status" className="text-xs text-muted-foreground">
                  This key predates capabilities and holds every capability except those that reach
                  inside a guest. Saving gives it exactly the ones selected.
                </p>
              )}
              <ApiKeyCapabilitySelector
                capabilities={capabilities}
                selected={selected}
                onToggle={toggle}
                disabled={isSubmitting}
              />
              {refusal && (
                <div role="alert" className="flex items-start gap-2 text-sm">
                  <XCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-status-offline" />
                  <p>{refusal}</p>
                </div>
              )}
              {warning && (
                <div role="alert" className="flex items-start gap-2 rounded-md border p-3 text-sm">
                  <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-status-pending" />
                  <div className="space-y-1">
                    <p className="font-medium">{warning.consequence}</p>
                    <p className="text-muted-foreground">{warning.message}</p>
                  </div>
                </div>
              )}
            </div>
          </DialogBody>
          <DialogFooter className="shrink-0 border-t pt-3">
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            {warning ? (
              <Button type="button" variant="destructive" disabled={isSubmitting} onClick={() => void save(true)}>
                {isSubmitting ? "Saving…" : "Withdraw Anyway"}
              </Button>
            ) : (
              <Button type="submit" disabled={isSubmitting || selected.length === 0}>
                {isSubmitting ? "Saving…" : "Save Capabilities"}
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
