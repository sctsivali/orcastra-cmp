/**
 * SPICE input helpers - keyboard scancode sending and keystroke paste.
 *
 * Pure functions over a live SpiceMainConn (no React). The library exposes its
 * message constructors as globals (window.SpiceMsgcKeyDown, etc.), so these
 * thin wrappers build and send the inputs-channel messages. Input is live as
 * soon as the inputs channel reports `ready`; there is no artificial gating.
 */

import {
  KEY_CODE_MAP,
  SHIFT_SYMBOLS,
  NUM_SHIFT_MAP,
  CHAR_TO_KEYSTROKE,
} from "./spice-keymap";

export interface KeyModifiers {
  ctrl?: boolean;
  alt?: boolean;
  shift?: boolean;
  meta?: boolean;
}

export interface AbortFlag {
  aborted: boolean;
}

export function isInputReady(sc: any): boolean {
  return !!(sc && sc.inputs && sc.inputs.state === "ready");
}

/** Send a single key down/up as a SPICE scancode message. */
export function sendScanKey(
  sc: any,
  keyCode: number,
  press = true,
  release = true,
): boolean {
  if (!isInputReady(sc)) return false;
  const win = window as any;
  if (!win.keycode_to_start_scan || !win.SpiceMsgcKeyDown || !win.SpiceMiniData) {
    return false;
  }
  try {
    if (press) {
      const down = new win.SpiceMsgcKeyDown();
      down.code = win.keycode_to_start_scan(keyCode);
      const msg = new win.SpiceMiniData();
      msg.build_msg(win.SPICE_MSGC_INPUTS_KEY_DOWN, down);
      sc.inputs.send_msg(msg);
    }
    if (release) {
      const up = new win.SpiceMsgcKeyUp();
      up.code = win.keycode_to_end_scan(keyCode);
      const msg = new win.SpiceMiniData();
      msg.build_msg(win.SPICE_MSGC_INPUTS_KEY_UP, up);
      sc.inputs.send_msg(msg);
    }
    return true;
  } catch {
    return false;
  }
}

/** Type a single character with the right Shift state. */
function sendChar(sc: any, char: string): void {
  const stroke = CHAR_TO_KEYSTROKE[char];
  if (stroke) {
    if (stroke.shift) sendScanKey(sc, 16, true, false);
    sendScanKey(sc, stroke.keyCode);
    if (stroke.shift) sendScanKey(sc, 16, false, true);
    return;
  }
  const code = char.toUpperCase().charCodeAt(0);
  if (code) sendScanKey(sc, code);
}

/** Send a named key ("Enter", "F2") or a literal string of characters. */
export function sendKeys(sc: any, keys: string): void {
  if (keys.toLowerCase() === "ctrl+alt+del") {
    sendCtrlAltDel(sc);
    return;
  }
  if (KEY_CODE_MAP[keys] !== undefined) {
    sendScanKey(sc, KEY_CODE_MAP[keys]);
    return;
  }
  for (const char of keys) sendChar(sc, char);
}

/** Send a key with explicit modifier keys held around it. */
export function sendKeyWithModifiers(
  sc: any,
  key: string,
  modifiers?: KeyModifiers,
): void {
  let keyCode: number;
  let needsShift = modifiers?.shift || false;

  if (NUM_SHIFT_MAP[key] !== undefined) {
    keyCode = NUM_SHIFT_MAP[key];
    needsShift = true;
  } else if (KEY_CODE_MAP[key] !== undefined) {
    keyCode = KEY_CODE_MAP[key];
    if (SHIFT_SYMBOLS.has(key)) needsShift = true;
  } else if (/^[a-zA-Z]$/.test(key)) {
    keyCode = key.toUpperCase().charCodeAt(0);
    if (key === key.toUpperCase() && key !== key.toLowerCase()) needsShift = true;
  } else if (/^[0-9]$/.test(key)) {
    keyCode = key.charCodeAt(0);
  } else {
    keyCode = key.toUpperCase().charCodeAt(0);
  }

  if (needsShift) sendScanKey(sc, 16, true, false);
  if (modifiers?.ctrl) sendScanKey(sc, 17, true, false);
  if (modifiers?.alt) sendScanKey(sc, 18, true, false);
  if (modifiers?.meta) sendScanKey(sc, 91, true, false);

  sendScanKey(sc, keyCode);

  if (modifiers?.meta) sendScanKey(sc, 91, false, true);
  if (modifiers?.alt) sendScanKey(sc, 18, false, true);
  if (modifiers?.ctrl) sendScanKey(sc, 17, false, true);
  if (needsShift) sendScanKey(sc, 16, false, true);
}

/** Send Ctrl+Alt+Del, preferring the library helper. */
export function sendCtrlAltDel(sc: any): void {
  const win = window as any;
  if (typeof win.sendCtrlAltDel === "function") {
    win.sendCtrlAltDel(sc);
    return;
  }
  sendScanKey(sc, 17, true, false); // Ctrl down
  sendScanKey(sc, 18, true, false); // Alt down
  sendScanKey(sc, 46, true, true); // Del press+release
  sendScanKey(sc, 18, false, true); // Alt up
  sendScanKey(sc, 17, false, true); // Ctrl up
}

// Gap between synthesized characters. The guest needs a brief moment to register
// each key transition; 8ms is well below human-perceptible yet reliable. The
// agent clipboard path (spice-clipboard) is instant and preferred when present.
const PASTE_CHAR_DELAY_MS = 8;

// Safety cap for keystroke paste. At ~8ms/char a large paste blocks the guest for
// many seconds (10k chars ≈ 80s) and looks like a hang. Cap it; callers should
// prefer the agent clipboard channel for bulk text. Exported so the UI can warn.
export const MAX_PASTE_KEYSTROKE_CHARS = 2000;

/**
 * Paste text by synthesizing keystrokes (US layout). Cancellable via an abort
 * flag so a disconnect or a second paste stops the loop. This is the fallback
 * for guests without spice-vdagent; prefer the agent clipboard channel.
 * Returns the number of characters actually typed (capped at
 * MAX_PASTE_KEYSTROKE_CHARS).
 */
export async function pasteViaKeystrokes(
  sc: any,
  text: string,
  signal?: AbortFlag,
): Promise<number> {
  const limit = Math.min(text.length, MAX_PASTE_KEYSTROKE_CHARS);
  if (text.length > MAX_PASTE_KEYSTROKE_CHARS) {
    console.warn(
      `[spice] keystroke paste truncated to ${MAX_PASTE_KEYSTROKE_CHARS} of ${text.length} chars; ` +
        "use the guest agent clipboard for bulk text",
    );
  }
  let sent = 0;
  for (let i = 0; i < limit; i++) {
    if (signal?.aborted) return sent;
    sendChar(sc, text[i]);
    sent++;
    if (i < limit - 1) {
      await new Promise((r) => setTimeout(r, PASTE_CHAR_DELAY_MS));
    }
  }
  return sent;
}
