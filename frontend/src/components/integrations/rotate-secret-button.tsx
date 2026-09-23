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
import type { APIKeyCreateResponse } from "@/types/api";

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
            immediately. Anything that uses this key, such as the OrcaHub connection, fails until
            you update it with the new secret.
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
