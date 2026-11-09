"use client";

import { AlertCircle, CheckCircle } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Card, CardTitle } from "@/components/ui/card";
import { IllustrationFallback } from "@/components/illustrations/illustration-frame";
import { KeysActiveIllustration, KeysExpiredIllustration } from "@/components/illustrations/scenes/kpi";

/** The two counters above the key table, from the full list so a tab cannot disagree with them. */
export function KeyStats({ activeCount, expiredCount }: { activeCount: number; expiredCount: number }) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card className="illo-kpi">
        <div className="flex items-start justify-between gap-4 p-6">
          <div className="min-w-0 space-y-2">
            <CardTitle className="text-sm font-medium">Active Keys</CardTitle>
            <div className="text-2xl font-bold">{activeCount}</div>
            <p className="text-xs text-muted-foreground">Valid and usable keys</p>
          </div>
          <IllustrationFallback>
            <CheckCircle className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          </IllustrationFallback>
          <KeysActiveIllustration size="sm" />
        </div>
      </Card>
      <Card className="illo-kpi">
        <div className="flex items-start justify-between gap-4 p-6">
          <div className="min-w-0 space-y-2">
            <CardTitle className="text-sm font-medium">Expired Keys</CardTitle>
            <div className="text-2xl font-bold">{expiredCount}</div>
            <p className="text-xs text-muted-foreground">Keys past expiration</p>
          </div>
          <IllustrationFallback>
            <AlertCircle className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          </IllustrationFallback>
          <KeysExpiredIllustration size="sm" />
        </div>
      </Card>
    </div>
  );
}

export function DeleteKeyDialog({
  keyName,
  open,
  onOpenChange,
  onConfirm,
}: {
  keyName: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete Key</AlertDialogTitle>
          <AlertDialogDescription>
            Are you sure you want to delete the key <strong className="text-foreground">&quot;{keyName}&quot;</strong>?
            This action cannot be undone and will permanently remove this TLS identity key. Its activity stays readable.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
