"use client";

import { useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
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
 * Issue a new secret for the same key id.
 *
 * note: the key id is what every synced organization records as its owner, so a rotation keeps
 * the organizations, the member push and provisioning working and replaces only the secret. Two
 * rotations in one tick would each show a secret while only the last one works, so the in-flight
 * flag is claimed synchronously rather than read from isPending.
 */
export function RotateSecretButton({
  keyId,
  keyName,
  onRotated,
}: {
  keyId: string;
  keyName: string;
  onRotated: (response: APIKeyCreateResponse) => void;
}) {
  const queryClient = useQueryClient();
  const rotateInFlightRef = useRef(false);
  const mutation = useMutation({
    mutationFn: () => api.integrations.rotateAPIKeySecret(keyId),
    onSuccess: (response) => {
      void reconcileMutationImpact(queryClient, "integration.write").catch(() => undefined);
      onRotated(response);
    },
    onError: (error: Error) => {
      toast({ title: "Failed to rotate the secret", description: error.message, variant: "destructive" });
    },
    onSettled: () => {
      rotateInFlightRef.current = false;
    },
  });

  const rotate = () => {
    if (rotateInFlightRef.current) return;
    rotateInFlightRef.current = true;
    mutation.mutate();
  };

  return (
    <AlertDialog>
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
        <AlertDialogHeader>
          <AlertDialogTitle>Rotate API Key Secret?</AlertDialogTitle>
          <AlertDialogDescription>
            A new secret is issued for {keyName}, under the same key ID, and the current secret
            stops working immediately. The organizations this key owns stay with it. The key&apos;s
            callback URL is cleared too, so a secret that leaked cannot be sent to an address
            someone else set; the connection sets it again on its next sync. Anything that uses
            this key, such as the OrcaHub connection, fails until you update it with the new secret.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={rotate}>Rotate Secret</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
