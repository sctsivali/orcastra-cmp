"use client";

import { useRef } from "react";
import { useMutation } from "@tanstack/react-query";
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
import { parseKeyTimestamp } from "@/lib/integration-display";
import type { APIKeyCreateResponse } from "@/types/api";

// note: the server refuses a revoked, inactive or expired key, so the control is not offered for
// one. A stored expiry without an offset is UTC, as the backend writes it.
export function isRotatable(key: { is_revoked?: boolean; is_active?: boolean; expires_at?: string | null }): boolean {
  if (key.is_revoked || key.is_active === false) return false;
  if (!key.expires_at) return true;
  const expires = parseKeyTimestamp(key.expires_at);
  return Number.isNaN(expires) ? false : expires > Date.now();
}

// note: a rotation keeps the key id, which every synced organization records as its owner, and
// replaces only the secret. Two rotations in one tick would each show a secret while only the
// last one works, so the in-flight flag is claimed synchronously, not through isPending.
export function RotateSecretButton({
  keyId,
  keyName,
  onRotated,
}: {
  keyId: string;
  keyName: string;
  onRotated: (response: APIKeyCreateResponse) => void;
}) {
  const inFlight = useRef(false);
  const mutation = useMutation({
    mutationFn: () => api.integrations.rotateAPIKeySecret(keyId),
    onSuccess: onRotated,
    onError: (error: Error) => {
      toast({ title: "Failed to rotate the secret", description: error.message, variant: "destructive" });
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const rotate = () => {
    if (inFlight.current) return;
    inFlight.current = true;
    mutation.mutate();
  };

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="outline" size="sm" disabled={mutation.isPending}>
          {mutation.isPending ? (
            <Loader2 className="h-4 w-4 mr-1 animate-spin" />
          ) : (
            <KeyRound className="h-4 w-4 mr-1" />
          )}
          Rotate secret
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Rotate the secret of {keyName}?</AlertDialogTitle>
          <AlertDialogDescription>
            A new secret is issued for the same key ID, and the current secret stops working
            immediately. The key&apos;s callback URL is cleared too, so a secret that leaked cannot
            be sent to an address someone else set; the connection sets it again on its next sync.
            Anything that uses this key, such as the OrcaHub connection, fails until you update it
            with the new secret.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={rotate}>Rotate secret</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
