/**
 * The task panel's persisted shell state.
 *
 * Shared by app/layout.tsx (server, first paint) and layout-provider.tsx (client, writes), so
 * this module imports nothing from components/. The cookie mirrors the sidebar's: the server
 * reads it so the strip is open or closed at the right height in the first byte of HTML.
 */

export const DOCK_COOKIE = "orcastra-dock";
export const DOCK_MIN = 160;
export const DOCK_DEFAULT = 320;
// note: the upper clamp only guards the cookie against a nonsense value. The live maximum is
// 70dvh, which the resize handle enforces against the real viewport.
const DOCK_MAX_STORED = 2000;

export type DockTab = "tasks" | "queue" | "log";

export interface DockState {
  open: boolean;
  height: number;
  tab: DockTab;
}

export const DOCK_DEFAULT_STATE: DockState = { open: false, height: DOCK_DEFAULT, tab: "tasks" };

const TABS: readonly DockTab[] = ["tasks", "queue", "log"];

function isDockTab(value: string | null): value is DockTab {
  return value !== null && (TABS as readonly string[]).includes(value);
}

export function clampDockHeight(px: number): number {
  if (!Number.isFinite(px)) return DOCK_DEFAULT;
  return Math.min(DOCK_MAX_STORED, Math.max(DOCK_MIN, Math.round(px)));
}

/** Never throws: a malformed cookie reads as the default. */
export function parseDockCookie(raw: string | undefined): DockState {
  if (!raw) return DOCK_DEFAULT_STATE;
  try {
    const params = new URLSearchParams(raw);
    const height = params.get("h");
    const tab = params.get("tab");
    return {
      open: params.get("open") === "1",
      height: height === null ? DOCK_DEFAULT : clampDockHeight(Number(height)),
      tab: isDockTab(tab) ? tab : "tasks",
    };
  } catch {
    return DOCK_DEFAULT_STATE;
  }
}

/** "open=1&h=320&tab=tasks" */
export function serialiseDockCookie(state: DockState): string {
  return `open=${state.open ? 1 : 0}&h=${clampDockHeight(state.height)}&tab=${state.tab}`;
}
