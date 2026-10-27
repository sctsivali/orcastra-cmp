import { fetchAPI } from "@/lib/api";

export type ConsoleAction = { host: string; project: string } & (
  | { action: "keys.send"; combo: string }
  | { action: "screenshot.take"; combo?: undefined }
);

export const consoleActionsAPI = {
  /**
   * Record a console toolbar action in the audit trail (backend routers/console_actions.py). The
   * keys already went to the guest over SPICE; this only says so, after the fact.
   */
  record: (instanceName: string, action: ConsoleAction) =>
    fetchAPI<void>(`/console/${encodeURIComponent(instanceName)}/actions`, {
      method: "POST",
      body: JSON.stringify(action),
    }),
};

/** Fire and forget: a failed audit write must never get in the way of the console. */
export function recordConsoleAction(instanceName: string, action: ConsoleAction): void {
  void consoleActionsAPI.record(instanceName, action).catch(() => undefined);
}
