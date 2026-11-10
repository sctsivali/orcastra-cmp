/** Terminal store actions with toast feedback. */

import { useCallback } from "react";
import { useTerminalStore, TerminalInstance } from "@/lib/terminal-store";
import { toast } from "@/hooks/use-toast";
import { plural } from "@/lib/utils";

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

  const openTerminal = useCallback(
    async ({ instanceName, host, project = "default" }: OpenTerminalParams): Promise<TerminalInstance | null> => {
      // The caller checks that the instance is running.
      const existing = getTerminalByInstance(instanceName, host);

      if (existing) {
        if (existing.minimized) {
          restoreTerminal(existing.id);
        } else {
          focusTerminal(existing.id);
        }
        toast({
          title: "Terminal focused",
          description: `Terminal for ${instanceName} is now focused.`,
        });
        return existing;
      }

      // May prompt for break-glass first; null means blocked, cancelled, or at the limit.
      const newTerminal = await storeOpenTerminal({ instanceName, host, project });

      if (newTerminal) {
        toast({
          title: "Terminal opened",
          description: `Connecting to ${instanceName}@${host}…`,
        });
      } else if (terminals.length >= useTerminalStore.getState().maxTerminals) {
        toast({
          title: "Could not open terminal",
          description: "Maximum number of terminals reached. Close some terminals first.",
          variant: "destructive",
        });
      }
      // Otherwise the break-glass prompt was cancelled and the modal already gave feedback.

      return newTerminal;
    },
    [storeOpenTerminal, getTerminalByInstance, restoreTerminal, focusTerminal, terminals.length]
  );

  const handleCloseTerminal = useCallback(
    (id: string) => {
      const terminal = getTerminal(id);
      closeTerminal(id);
      if (terminal) {
        toast({
          title: "Terminal closed",
          description: `Terminal for ${terminal.instanceName} has been closed.`,
        });
      }
    },
    [closeTerminal, getTerminal]
  );

  const handleCloseAllTerminals = useCallback(() => {
    const count = terminals.length;
    closeAllTerminals();
    if (count > 0) {
      toast({
        title: "All terminals closed",
        description: `${plural(count, "terminal has", "terminals have")} been closed.`,
      });
    }
  }, [closeAllTerminals, terminals.length]);

  const hasTerminal = useCallback(
    (instanceName: string, host: string): boolean => {
      return !!getTerminalByInstance(instanceName, host);
    },
    [getTerminalByInstance]
  );

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
