/**
 * Sending keys to a guest over an open SPICE connection (the vendored spice-html5 `SpiceMainConn`).
 *
 * The message classes come from the vendored scripts, loaded as globals by the console. Nothing
 * here touches the DOM's keyboard events, so it works the same from a toolbar button, a menu or
 * the on-screen keyboard.
 */

import { breakCode, makeCode, type KeyCombo, type ScanKey } from "./keys";

interface SpiceGlobals {
  SpiceMsgcKeyDown: new () => { code: number };
  SpiceMsgcKeyUp: new () => { code: number };
  SpiceMiniData: new () => { build_msg: (type: number, body: unknown) => void };
  SPICE_MSGC_INPUTS_KEY_DOWN: number;
  SPICE_MSGC_INPUTS_KEY_UP: number;
}

export interface SpiceInputConnection {
  inputs?: { state?: string; send_msg: (msg: unknown) => void } | null;
}

/** How long a combination is held before release; Windows ignores a Win+L shorter than a few ms. */
export const HOLD_MS = 50;

function globals(): SpiceGlobals | null {
  const win = (typeof window === "undefined" ? undefined : window) as unknown as Partial<SpiceGlobals> | undefined;
  if (!win?.SpiceMsgcKeyDown || !win.SpiceMsgcKeyUp || !win.SpiceMiniData) return null;
  return win as SpiceGlobals;
}

export function inputsReady(conn: SpiceInputConnection | null | undefined): boolean {
  return Boolean(conn?.inputs && conn.inputs.state === "ready");
}

function send(conn: SpiceInputConnection, g: SpiceGlobals, key: ScanKey, down: boolean): void {
  const body = down ? new g.SpiceMsgcKeyDown() : new g.SpiceMsgcKeyUp();
  body.code = down ? makeCode(key) : breakCode(key);
  const msg = new g.SpiceMiniData();
  msg.build_msg(down ? g.SPICE_MSGC_INPUTS_KEY_DOWN : g.SPICE_MSGC_INPUTS_KEY_UP, body);
  conn.inputs!.send_msg(msg);
}

const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Press every key of the combination in order, hold, then release them in reverse. False when the
 * connection is not ready or the SPICE scripts are not loaded; nothing is sent then.
 */
export async function sendCombo(conn: SpiceInputConnection | null | undefined, combo: Pick<KeyCombo, "keys">): Promise<boolean> {
  const g = globals();
  if (!conn || !g || !inputsReady(conn)) return false;
  const pressed: ScanKey[] = [];
  try {
    for (const key of combo.keys) {
      send(conn, g, key, true);
      pressed.push(key);
    }
    await pause(HOLD_MS);
    return true;
  } finally {
    // Released even when a send threw part way, so the guest is never left holding Ctrl or Win.
    for (const key of pressed.reverse()) {
      try {
        if (inputsReady(conn)) send(conn, g, key, false);
      } catch {
        // The connection went away; the guest releases on its own when the channel closes.
      }
    }
  }
}
