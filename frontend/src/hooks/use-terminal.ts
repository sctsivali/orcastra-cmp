/**
 * useTerminal Hook - Convenience hook for terminal operations.
 * Provides easy access to terminal store actions.
 */

import { useCallback } from "react";
import { useTerminalStore, TerminalInstance } from "@/lib/terminal-store";
import { toast } from "@/hooks/use-toast";

interface OpenTerminalParams {
  instanceName: string;
  host: string;
  project?: string;
}

export function useTerminal() {
  const {
    terminals,
    openTerminal: storeOpenTerminal,
    closeTerminal,
    closeAllTerminals,
    minimizeTerminal,
    restoreTerminal,
    focusTerminal,
    getTerminal,
    getTerminalByInstance,
  } = useTerminalStore();

  // Open terminal with toast notification
  const openTerminal = useCallback(
    async ({ instanceName, host, project = "default" }: OpenTerminalParams): Promise<TerminalInstance | null> => {
      // Check if instance is running (caller should validate this)
      const existing = getTerminalByInstance(instanceName, host);

      if (existing) {
        // Focus existing terminal
        if (existing.minimized) {
          restoreTerminal(existing.id);
        } else {
          focusTerminal(existing.id);
        }
        toast({
          title: "Terminal Focused",
          description: `Terminal for ${instanceName} is now focused.`,
        });
        return existing;
      }

      // Open new terminal (may prompt for break-glass first; null = blocked, cancelled, or at limit)
      const newTerminal = await storeOpenTerminal({ instanceName, host, project });

      if (newTerminal) {
        toast({
          title: "Terminal Opened",
          description: `Connecting to ${instanceName}@${host}…`,
        });
      } else if (terminals.length >= useTerminalStore.getState().maxTerminals) {
        toast({
          title: "Cannot Open Terminal",
          description: "Maximum number of terminals reached. Close some terminals first.",
          variant: "destructive",
        });
      }
      // else: break-glass prompt was cancelled - the modal already gave feedback.

      return newTerminal;
    },
    [storeOpenTerminal, getTerminalByInstance, restoreTerminal, focusTerminal, terminals.length]
  );

  // Close terminal with toast
  const handleCloseTerminal = useCallback(
    (id: string) => {
      const terminal = getTerminal(id);
      closeTerminal(id);
      if (terminal) {
        toast({
          title: "Terminal Closed",
          description: `Terminal for ${terminal.instanceName} has been closed.`,
        });
      }
    },
    [closeTerminal, getTerminal]
  );

  // Close all terminals
  const handleCloseAllTerminals = useCallback(() => {
    const count = terminals.length;
    closeAllTerminals();
    if (count > 0) {
      toast({
        title: "All Terminals Closed",
        description: `${count} terminal(s) have been closed.`,
      });
    }
  }, [closeAllTerminals, terminals.length]);

  // Check if terminal exists for instance
  const hasTerminal = useCallback(
    (instanceName: string, host: string): boolean => {
      return !!getTerminalByInstance(instanceName, host);
    },
    [getTerminalByInstance]
  );

  // Get terminal count
  const terminalCount = terminals.length;
  const activeCount = terminals.filter((t) => !t.minimized).length;
  const minimizedCount = terminals.filter((t) => t.minimized).length;

  return {
    terminals,
    terminalCount,
    activeCount,
    minimizedCount,
    openTerminal,
    closeTerminal: handleCloseTerminal,
    closeAllTerminals: handleCloseAllTerminals,
    minimizeTerminal,
    restoreTerminal,
    focusTerminal,
    getTerminal,
    hasTerminal,
  };
}

export default useTerminal;
