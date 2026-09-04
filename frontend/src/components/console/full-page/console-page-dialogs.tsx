/**
 * Confirmation and session-conflict dialogs for the full-page console route.
 * Pure presentational: state and actions live in the page.
 */

"use client";

import * as React from "react";
import {  } from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
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
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";
import type { ConsoleOperation } from "./console-page-toolbar";

export interface ConflictSession {
  operation_id: string;
  instance_name: string;
  description: string;
  created_at: string;
  may_cancel: boolean;
}

interface ConsolePageDialogsProps {
  instanceName: string;
  confirmDialog: { open: boolean; operation: ConsoleOperation | null; title: string; description: string };
  onConfirmOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  sessionConflict: { show: boolean; sessions: ConflictSession[] };
  killingSession: boolean;
  onKillAll: () => void;
  onContinueAnyway: () => void;
  onCancelConflict: () => void;
}

export function ConsolePageDialogs({
  instanceName,
  confirmDialog,
  onConfirmOpenChange,
  onConfirm,
  sessionConflict,
  killingSession,
  onKillAll,
  onContinueAnyway,
  onCancelConflict,
}: ConsolePageDialogsProps) {
  return (
    <>
      <AlertDialog open={confirmDialog.open} onOpenChange={onConfirmOpenChange}>
        <AlertDialogContent className="bg-terminal border-terminal-border">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-gray-200">{confirmDialog.title}</AlertDialogTitle>
            <AlertDialogDescription className="text-gray-400">{confirmDialog.description}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="bg-transparent border-gray-600 text-gray-300 hover:bg-gray-800">Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={onConfirm}
              className={cn(confirmDialog.operation === "stop" && "bg-red-600 hover:bg-red-700", confirmDialog.operation === "restart" && "bg-blue-600 hover:bg-blue-700")}
            >
              {confirmDialog.operation === "stop" ? "Stop" : "Restart"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={sessionConflict.show}>
        <AlertDialogContent className="bg-terminal border-terminal-border max-w-lg">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-yellow-400">Active Console Session Detected</AlertDialogTitle>
            <AlertDialogDescription className="text-gray-300">
              Another console session is already running for <strong>{instanceName}</strong>.
              <br />
              Running multiple console sessions may cause display issues. You can:
              <ul className="list-disc list-inside mt-2 text-sm">
                <li>Kill the existing session and continue</li>
                <li>Continue anyway (not recommended)</li>
                <li>Cancel and use the existing session</li>
              </ul>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="max-h-40 overflow-y-auto space-y-2 my-2">
            {sessionConflict.sessions.map((session) => (
              <div key={session.operation_id} className="flex items-center justify-between p-2 bg-gray-800/50 rounded text-sm">
                <div className="flex-1 min-w-0">
                  <div className="text-gray-200 truncate">{session.operation_id.slice(0, 12)}...</div>
                  <div className="text-gray-500 text-xs">{session.description}</div>
                </div>
                <div className="text-gray-400 text-xs ml-2">
                  {new Date(session.created_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                </div>
              </div>
            ))}
          </div>
          <AlertDialogFooter className="flex gap-2">
            <AlertDialogCancel className="bg-transparent border-gray-600 text-gray-300 hover:bg-gray-800" onClick={onCancelConflict}>
              Cancel
            </AlertDialogCancel>
            <Button variant="destructive" onClick={onKillAll} disabled={killingSession}>
              {killingSession && <Spinner className="h-4 w-4 mr-2" />}
              Kill &amp; Continue
            </Button>
            <Button variant="outline" className="border-yellow-500/50 text-yellow-400 hover:bg-yellow-500/10" onClick={onContinueAnyway}>
              Continue Anyway
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export default ConsolePageDialogs;
