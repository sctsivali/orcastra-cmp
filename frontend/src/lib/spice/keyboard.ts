/**
 * The graphical console's keyboard: physical keys to PC/XT set 1 scancodes.
 *
 * important: keys are mapped by `KeyboardEvent.code`, the physical key, not by `keyCode`. A remote
 * console sends what a real keyboard would and lets the guest apply its own layout, which is how
 * an AZERTY or German keyboard types the right characters and AltGr works. The vendored mapping
 * went by `keyCode`, which depends on the host's layout, folded right Ctrl/Alt into the left
 * ones, and encoded extended keys with a stray third byte (Print Screen sent a backtick).
 *
 * important: only the console whose canvas has focus sends keys. Each console used to listen on
 * `window` and forward anything typed outside its own canvas, so with two consoles open, keys
 * typed into one (passwords included) also reached the other VM.
 */

/** [set 1 make code, extended (0xE0 prefixed)] per KeyboardEvent.code. */
const CODES: Record<string, readonly [number, boolean]> = {
  Escape: [0x01, false],
  Digit1: [0x02, false], Digit2: [0x03, false], Digit3: [0x04, false], Digit4: [0x05, false],
  Digit5: [0x06, false], Digit6: [0x07, false], Digit7: [0x08, false], Digit8: [0x09, false],
  Digit9: [0x0a, false], Digit0: [0x0b, false],
  Minus: [0x0c, false], Equal: [0x0d, false], Backspace: [0x0e, false], Tab: [0x0f, false],
  KeyQ: [0x10, false], KeyW: [0x11, false], KeyE: [0x12, false], KeyR: [0x13, false],
  KeyT: [0x14, false], KeyY: [0x15, false], KeyU: [0x16, false], KeyI: [0x17, false],
  KeyO: [0x18, false], KeyP: [0x19, false],
  BracketLeft: [0x1a, false], BracketRight: [0x1b, false], Enter: [0x1c, false],
  ControlLeft: [0x1d, false],
  KeyA: [0x1e, false], KeyS: [0x1f, false], KeyD: [0x20, false], KeyF: [0x21, false],
  KeyG: [0x22, false], KeyH: [0x23, false], KeyJ: [0x24, false], KeyK: [0x25, false],
  KeyL: [0x26, false],
  Semicolon: [0x27, false], Quote: [0x28, false], Backquote: [0x29, false],
  ShiftLeft: [0x2a, false], Backslash: [0x2b, false],
  KeyZ: [0x2c, false], KeyX: [0x2d, false], KeyC: [0x2e, false], KeyV: [0x2f, false],
  KeyB: [0x30, false], KeyN: [0x31, false], KeyM: [0x32, false],
  Comma: [0x33, false], Period: [0x34, false], Slash: [0x35, false], ShiftRight: [0x36, false],
  NumpadMultiply: [0x37, false], AltLeft: [0x38, false], Space: [0x39, false], CapsLock: [0x3a, false],
  F1: [0x3b, false], F2: [0x3c, false], F3: [0x3d, false], F4: [0x3e, false], F5: [0x3f, false],
  F6: [0x40, false], F7: [0x41, false], F8: [0x42, false], F9: [0x43, false], F10: [0x44, false],
  NumLock: [0x45, false], ScrollLock: [0x46, false],
  Numpad7: [0x47, false], Numpad8: [0x48, false], Numpad9: [0x49, false], NumpadSubtract: [0x4a, false],
  Numpad4: [0x4b, false], Numpad5: [0x4c, false], Numpad6: [0x4d, false], NumpadAdd: [0x4e, false],
  Numpad1: [0x4f, false], Numpad2: [0x50, false], Numpad3: [0x51, false], Numpad0: [0x52, false],
  NumpadDecimal: [0x53, false], IntlBackslash: [0x56, false], F11: [0x57, false], F12: [0x58, false],
  F13: [0x64, false], F14: [0x65, false], F15: [0x66, false], F16: [0x67, false], F17: [0x68, false],
  F18: [0x69, false], F19: [0x6a, false], F20: [0x6b, false], F21: [0x6c, false], F22: [0x6d, false],
  F23: [0x6e, false], F24: [0x76, false],
  KanaMode: [0x70, false], IntlRo: [0x73, false], Convert: [0x79, false], NonConvert: [0x7b, false],
  IntlYen: [0x7d, false], NumpadComma: [0x7e, false],
  NumpadEnter: [0x1c, true], ControlRight: [0x1d, true], NumpadDivide: [0x35, true],
  PrintScreen: [0x37, true], AltRight: [0x38, true],
  Home: [0x47, true], ArrowUp: [0x48, true], PageUp: [0x49, true], ArrowLeft: [0x4b, true],
  ArrowRight: [0x4d, true], End: [0x4f, true], ArrowDown: [0x50, true], PageDown: [0x51, true],
  Insert: [0x52, true], Delete: [0x53, true],
  MetaLeft: [0x5b, true], MetaRight: [0x5c, true], OSLeft: [0x5b, true], OSRight: [0x5c, true],
  ContextMenu: [0x5d, true], Power: [0x5e, true], Sleep: [0x5f, true], WakeUp: [0x63, true],
  AudioVolumeMute: [0x20, true], AudioVolumeDown: [0x2e, true], AudioVolumeUp: [0x30, true],
  MediaPlayPause: [0x22, true], MediaStop: [0x24, true],
  MediaTrackNext: [0x19, true], MediaTrackPrevious: [0x10, true],
};

/** Pause sends a make sequence only: E1 1D 45, then E1 9D C5. */
const PAUSE_DOWN = 0xe1 | (0x1d << 8) | (0x45 << 16);
const PAUSE_UP = 0xe1 | (0x9d << 8) | (0xc5 << 16);

/** The 32-bit code of a SpiceMsgcKeyDown (bytes first-lowest), or null for an unmapped key. */
export function downCode(code: string): number | null {
  if (code === "Pause") return PAUSE_DOWN;
  const entry = CODES[code];
  if (!entry) return null;
  const [make, extended] = entry;
  return extended ? 0xe0 | (make << 8) : make;
}

/** The SpiceMsgcKeyUp code: the make code with its high bit set. */
export function upCode(code: string): number | null {
  if (code === "Pause") return PAUSE_UP;
  const entry = CODES[code];
  if (!entry) return null;
  const [make, extended] = entry;
  return extended ? 0xe0 | ((make | 0x80) << 8) : make | 0x80;
}

export function isMappedKey(code: string): boolean {
  return code === "Pause" || code in CODES;
}

/** Characters typed with Shift on a US layout, by the key that types them. */
const SHIFTED: Record<string, string> = {
  "!": "Digit1", "@": "Digit2", "#": "Digit3", "$": "Digit4", "%": "Digit5", "^": "Digit6",
  "&": "Digit7", "*": "Digit8", "(": "Digit9", ")": "Digit0", "_": "Minus", "+": "Equal",
  "{": "BracketLeft", "}": "BracketRight", "|": "Backslash", ":": "Semicolon", '"': "Quote",
  "~": "Backquote", "<": "Comma", ">": "Period", "?": "Slash",
};
const PLAIN: Record<string, string> = {
  "-": "Minus", "=": "Equal", "[": "BracketLeft", "]": "BracketRight", "\\": "Backslash",
  ";": "Semicolon", "'": "Quote", "`": "Backquote", ",": "Comma", ".": "Period", "/": "Slash",
  " ": "Space", "\n": "Enter", "\r": "Enter", "\t": "Tab",
};

/**
 * The key and Shift state that type `char` on a US layout guest, or null for a character a US
 * keyboard has no key for. Used by typed paste and the on-screen keyboard.
 */
export function keyForCharacter(char: string): { code: string; shift: boolean } | null {
  if (/^[a-z]$/.test(char)) return { code: `Key${char.toUpperCase()}`, shift: false };
  if (/^[A-Z]$/.test(char)) return { code: `Key${char}`, shift: true };
  if (/^[0-9]$/.test(char)) return { code: `Digit${char}`, shift: false };
  if (PLAIN[char]) return { code: PLAIN[char], shift: false };
  if (SHIFTED[char]) return { code: SHIFTED[char], shift: true };
  return null;
}

/** Named keys of the on-screen keyboard and the BIOS menu, as KeyboardEvent.code values. */
export const NAMED_KEYS: Record<string, string> = {
  Escape: "Escape", Esc: "Escape", Enter: "Enter", Tab: "Tab", Backspace: "Backspace",
  Space: "Space", Delete: "Delete", Del: "Delete", Insert: "Insert", Ins: "Insert",
  Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown",
  ArrowUp: "ArrowUp", ArrowDown: "ArrowDown", ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight",
  Shift: "ShiftLeft", Control: "ControlLeft", Ctrl: "ControlLeft", Alt: "AltLeft", Meta: "MetaLeft",
  CapsLock: "CapsLock", NumLock: "NumLock", ScrollLock: "ScrollLock",
  PrintScreen: "PrintScreen", Pause: "Pause",
  F1: "F1", F2: "F2", F3: "F3", F4: "F4", F5: "F5", F6: "F6",
  F7: "F7", F8: "F8", F9: "F9", F10: "F10", F11: "F11", F12: "F12",
};

export type SendScancode = (code: number, down: boolean) => boolean;

/**
 * Forwards one canvas's keyboard to the guest while it has focus, and releases every key it
 * pressed when focus leaves (Alt+Tab away used to leave Alt held in the guest).
 */
export class KeyboardBridge {
  private readonly pressed = new Set<string>();
  private readonly target: HTMLElement;
  private readonly send: SendScancode;

  constructor(target: HTMLElement, send: SendScancode) {
    this.target = target;
    this.send = send;
    target.addEventListener("keydown", this.onKeyDown);
    target.addEventListener("keyup", this.onKeyUp);
    target.addEventListener("blur", this.releaseAll);
    window.addEventListener("blur", this.releaseAll);
    document.addEventListener("visibilitychange", this.onVisibility);
  }

  dispose(): void {
    this.releaseAll();
    this.target.removeEventListener("keydown", this.onKeyDown);
    this.target.removeEventListener("keyup", this.onKeyUp);
    this.target.removeEventListener("blur", this.releaseAll);
    window.removeEventListener("blur", this.releaseAll);
    document.removeEventListener("visibilitychange", this.onVisibility);
  }

  /** Keys currently held in the guest by this bridge (for tests and the release path). */
  get held(): number {
    return this.pressed.size;
  }

  releaseAll = (): void => {
    for (const code of Array.from(this.pressed).reverse()) {
      const up = upCode(code);
      if (up !== null) this.send(up, false);
    }
    this.pressed.clear();
  };

  private onVisibility = (): void => {
    if (document.hidden) this.releaseAll();
  };

  private onKeyDown = (event: KeyboardEvent): void => {
    // An IME composition belongs to the host; the guest gets the result through paste.
    if (event.isComposing || event.keyCode === 229) return;
    const down = downCode(event.code);
    if (down === null) return;
    event.preventDefault();
    event.stopPropagation();
    // note: auto-repeat is forwarded. A held key's repeated make codes are what PS/2 guests
    // repeat on, and evdev-backed guests ignore a press of a key already down.
    if (this.send(down, true)) this.pressed.add(event.code);
  };

  private onKeyUp = (event: KeyboardEvent): void => {
    const up = upCode(event.code);
    if (up === null) return;
    event.preventDefault();
    event.stopPropagation();
    // Print Screen often arrives as a keyup alone (the host takes the keydown).
    if (event.code === "PrintScreen" && !this.pressed.has(event.code)) {
      const down = downCode(event.code);
      if (down !== null) this.send(down, true);
    }
    this.send(up, false);
    this.pressed.delete(event.code);
  };
}
