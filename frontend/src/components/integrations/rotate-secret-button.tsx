"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type { APIKeyCreateResponse } from "@/types/api";

/**
 * Issue a new secret for the same key id, once its name is typed.
 *
 * Synced organizations record the key id as their owner, so a rotation keeps them working and
 * replaces only the secret. It does stop whatever uses the key until it has the new secret, so
 * the name is typed first. Two rotations in one tick would each show a secret while only the
 * last one works, so the in-flight flag is claimed synchronously instead of read from isPending.
 */
export function RotateSecretButton({
  keyId,
  keyName,
  onRotated,
}: {
  keyId: string;
  /** What the operator types to confirm: the key's name, or its id when it has none. */
  keyName: string;
  onRotated: (response: APIKeyCreateResponse) => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const rotateInFlightRef = useRef(false);
  const mutation = useMutation({
    mutationFn: () => api.integrations.rotateAPIKeySecret(keyId),
    onSuccess: (response) => {
      void reconcileMutationImpact(queryClient, "integration.write").catch(() => undefined);
      setOpen(false);
      onRotated(response);
    },
    onError: (error: Error) => {
      toast({ title: "Could not rotate secret", description: error.message, variant: "destructive" });
    },
    onSettled: () => {
      rotateInFlightRef.current = false;
    },
  });

  const confirmed = confirmation.trim() === keyName;

  const rotate = (event: React.FormEvent) => {
    event.preventDefault();
    if (rotateInFlightRef.current) return;
    if (!confirmed) return;
    rotateInFlightRef.current = true;
    mutation.mutate();
  };

  const changeOpen = (next: boolean) => {
    setOpen(next);
    if (next) setConfirmation("");
  };

  return (
    <AlertDialog open={open} onOpenChange={changeOpen}>
      <AlertDialogTrigger asChild>
        <Button variant="outline" size="sm" disabled={mutation.isPending}>
          {mutation.isPending ? (
            <Loader2 className="h-4 w-4 mr-1 animate-spin" aria-hidden />
          ) : (
            <KeyRound className="h-4 w-4 mr-1" aria-hidden />
          )}
          Rotate Secret
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <form onSubmit={rotate} className="grid gap-4">
          <AlertDialogHeader>
            <AlertDialogTitle>Rotate API Key Secret?</AlertDialogTitle>
            <AlertDialogDescription>
              A new secret is issued for {keyName}, under the same key ID, and the current secret
              stops working. The organizations this key owns stay with it. The key&apos;s callback URL
              is cleared too, so a secret that leaked cannot be sent to an address someone else set;
              the connection sets it again on its next sync. Anything that uses this key, such as the
              OrcaHub connection, fails until you update it with the new secret.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2">
            <Label htmlFor={`rotate-confirm-${keyId}`}>Type the key&apos;s name to confirm</Label>
            <Input
              id={`rotate-confirm-${keyId}`}
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              placeholder={keyName}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel type="button">Cancel</AlertDialogCancel>
            <Button type="submit" disabled={!confirmed || mutation.isPending}>
              {mutation.isPending && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
              Rotate Secret
            </Button>
          </AlertDialogFooter>
        </form>
      </AlertDialogContent>
    </AlertDialog>
  );
}
