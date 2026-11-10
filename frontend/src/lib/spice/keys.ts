/**
 * The keys and key combinations the console sends to a guest over SPICE.
 *
 * Combinations are sent as PC/XT set 1 scancodes, the codes a real keyboard sends and what QEMU's
 * SPICE input channel expects, rather than through the browser's keyCode. The vendored
 * spice-html5 maps keyCodes per browser and gets some wrong (Print Screen), and its
 * sendCtrlAltDel throws in strict code, so Ctrl+Alt+Del never reaches a guest through it.
 *
 * Typing and paste go through lib/spice/keyboard.ts (keyForCharacter, NAMED_KEYS).
 */

export type ScanKey =
  | "ctrl" | "alt" | "shift" | "win"
  | "escape" | "tab" | "enter" | "space" | "backspace" | "delete" | "insert"
  | "home" | "end" | "pageup" | "pagedown" | "up" | "down" | "left" | "right" | "printscreen"
  | "f1" | "f2" | "f3" | "f4" | "f5" | "f6" | "f7" | "f8" | "f9" | "f10" | "f11" | "f12"
  | "a" | "b" | "c" | "d" | "e" | "f" | "g" | "h" | "i" | "j" | "k" | "l" | "m"
  | "n" | "o" | "p" | "q" | "r" | "s" | "t" | "u" | "v" | "w" | "x" | "y" | "z"
  | "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9";

/** Set 1 make codes. `extended` keys are sent with the 0xE0 prefix. */
export const SCANCODES: Record<ScanKey, { code: number; extended: boolean }> = {
  ctrl: { code: 0x1d, extended: false },
  alt: { code: 0x38, extended: false },
  shift: { code: 0x2a, extended: false },
  win: { code: 0x5b, extended: true },
  escape: { code: 0x01, extended: false },
  tab: { code: 0x0f, extended: false },
  enter: { code: 0x1c, extended: false },
  space: { code: 0x39, extended: false },
  backspace: { code: 0x0e, extended: false },
  delete: { code: 0x53, extended: true },
  insert: { code: 0x52, extended: true },
  home: { code: 0x47, extended: true },
  end: { code: 0x4f, extended: true },
  pageup: { code: 0x49, extended: true },
  pagedown: { code: 0x51, extended: true },
  up: { code: 0x48, extended: true },
  down: { code: 0x50, extended: true },
  left: { code: 0x4b, extended: true },
  right: { code: 0x4d, extended: true },
  printscreen: { code: 0x37, extended: true },
  f1: { code: 0x3b, extended: false }, f2: { code: 0x3c, extended: false },
  f3: { code: 0x3d, extended: false }, f4: { code: 0x3e, extended: false },
  f5: { code: 0x3f, extended: false }, f6: { code: 0x40, extended: false },
  f7: { code: 0x41, extended: false }, f8: { code: 0x42, extended: false },
  f9: { code: 0x43, extended: false }, f10: { code: 0x44, extended: false },
  f11: { code: 0x57, extended: false }, f12: { code: 0x58, extended: false },
  q: { code: 0x10, extended: false }, w: { code: 0x11, extended: false }, e: { code: 0x12, extended: false },
  r: { code: 0x13, extended: false }, t: { code: 0x14, extended: false }, y: { code: 0x15, extended: false },
  u: { code: 0x16, extended: false }, i: { code: 0x17, extended: false }, o: { code: 0x18, extended: false },
  p: { code: 0x19, extended: false }, a: { code: 0x1e, extended: false }, s: { code: 0x1f, extended: false },
  d: { code: 0x20, extended: false }, f: { code: 0x21, extended: false }, g: { code: 0x22, extended: false },
  h: { code: 0x23, extended: false }, j: { code: 0x24, extended: false }, k: { code: 0x25, extended: false },
  l: { code: 0x26, extended: false }, z: { code: 0x2c, extended: false }, x: { code: 0x2d, extended: false },
  c: { code: 0x2e, extended: false }, v: { code: 0x2f, extended: false }, b: { code: 0x30, extended: false },
  n: { code: 0x31, extended: false }, m: { code: 0x32, extended: false },
  "1": { code: 0x02, extended: false }, "2": { code: 0x03, extended: false }, "3": { code: 0x04, extended: false },
  "4": { code: 0x05, extended: false }, "5": { code: 0x06, extended: false }, "6": { code: 0x07, extended: false },
  "7": { code: 0x08, extended: false }, "8": { code: 0x09, extended: false }, "9": { code: 0x0a, extended: false },
  "0": { code: 0x0b, extended: false },
};

/** The 32-bit code a SpiceMsgcKeyDown carries: the scancode bytes, first byte lowest. */
export function makeCode(key: ScanKey): number {
  const { code, extended } = SCANCODES[key];
  return extended ? 0xe0 | (code << 8) : code;
}

/** The SpiceMsgcKeyUp code: the make code with its high bit set. */
export function breakCode(key: ScanKey): number {
  const { code, extended } = SCANCODES[key];
  return extended ? 0xe0 | ((code | 0x80) << 8) : code | 0x80;
}

export interface KeyCombo {
  id: string;
  label: string;
  /** Pressed in this order, released in reverse. */
  keys: ScanKey[];
  hint?: string;
  /** A Linux guest reboots on Ctrl+Alt+Del (systemd's ctrl-alt-del.target), so ask first. */
  confirmUnlessWindows?: boolean;
  group: "system" | "windows" | "terminals";
}

const TERMINALS: KeyCombo[] = Array.from({ length: 12 }, (_, i) => ({
  id: `ctrl-alt-f${i + 1}`,
  label: `Ctrl+Alt+F${i + 1}`,
  keys: ["ctrl", "alt", `f${i + 1}` as ScanKey],
  hint: i === 0 ? "Linux: switch to virtual terminal 1 (often the graphical session)" : `Linux: switch to virtual terminal ${i + 1}`,
  group: "terminals" as const,
}));

/** The named combinations of the Send Keys menu. The ids are recorded in the audit trail. */
export const COMBOS: KeyCombo[] = [
  {
    id: "ctrl-alt-del", label: "Ctrl+Alt+Del", keys: ["ctrl", "alt", "delete"],
    hint: "Windows: sign in, lock or open the security screen", confirmUnlessWindows: true, group: "system",
  },
  { id: "lock", label: "Lock Screen (Win+L)", keys: ["win", "l"], hint: "Windows: lock the session", group: "windows" },
  {
    id: "switch-user", label: "Switch User", keys: ["ctrl", "alt", "delete"],
    hint: "Opens the Windows security screen; choose Switch user there", confirmUnlessWindows: true, group: "windows",
  },
  { id: "task-manager", label: "Task Manager (Ctrl+Shift+Esc)", keys: ["ctrl", "shift", "escape"], group: "windows" },
  { id: "alt-tab", label: "Alt+Tab", keys: ["alt", "tab"], hint: "Switch windows", group: "system" },
  { id: "alt-f4", label: "Alt+F4", keys: ["alt", "f4"], hint: "Close the active window", group: "system" },
  { id: "win", label: "Windows Key", keys: ["win"], hint: "Open the Start menu", group: "windows" },
  { id: "win-r", label: "Run (Win+R)", keys: ["win", "r"], group: "windows" },
  { id: "print-screen", label: "Print Screen", keys: ["printscreen"], hint: "The guest's own screenshot key", group: "system" },
  ...TERMINALS,
];

export const COMBO_BY_ID: Record<string, KeyCombo> = Object.fromEntries(COMBOS.map((c) => [c.id, c]));

export const MODIFIERS = ["ctrl", "alt", "shift", "win"] as const;
export type Modifier = (typeof MODIFIERS)[number];

/** Keys a custom combination may end in: everything but the modifiers. */
export const COMBO_KEYS = (Object.keys(SCANCODES) as ScanKey[]).filter((k) => !(MODIFIERS as readonly string[]).includes(k));

/** A custom combination as the audit trail records it: modifiers in a fixed order, then the key. */
export function customCombo(modifiers: Modifier[], key: ScanKey): KeyCombo {
  const ordered = MODIFIERS.filter((m) => modifiers.includes(m));
  const id = [...ordered, key].join("+");
  return {
    id,
    label: [...ordered, key].map((part) => (part.length === 1 ? part.toUpperCase() : part[0].toUpperCase() + part.slice(1))).join("+"),
    keys: [...ordered, key],
    confirmUnlessWindows: ordered.includes("ctrl") && ordered.includes("alt") && key === "delete",
    group: "system",
  };
}

/** Whether an image's os property names Windows; unknown is treated as not Windows. */
export function isWindowsGuest(os: string | null | undefined): boolean {
  return /windows/i.test(os ?? "");
}

/**
 * DOM keyCodes by key name and character, read by characterKey. Typing, paste, BIOS keys and the
 * on-screen keyboard use KeyboardEvent.code values from lib/spice/keyboard.ts instead.
 */
export const KEY_CODES: Record<string, number> = {
  Escape: 27, Esc: 27,
  Enter: 13, Tab: 9, Backspace: 8, Space: 32, " ": 32,
  Delete: 46, Del: 46, Insert: 45, Ins: 45,
  Home: 36, End: 35, PageUp: 33, PageDown: 34,
  ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39,
  Shift: 16, Control: 17, Ctrl: 17, Alt: 18, Meta: 91,
  CapsLock: 20, NumLock: 144, ScrollLock: 145,
  PrintScreen: 44, Pause: 19,
  F1: 112, F2: 113, F3: 114, F4: 115, F5: 116, F6: 117,
  F7: 118, F8: 119, F9: 120, F10: 121, F11: 122, F12: 123,
  ";": 186, ":": 186, "=": 187, "+": 187, ",": 188, "<": 188,
  "-": 189, "_": 189, ".": 190, ">": 190, "/": 191, "?": 191,
  "`": 192, "~": 192, "[": 219, "{": 219, "\\": 220, "|": 220,
  "]": 221, "}": 221, "'": 222, '"': 222,
};

/** Characters typed with Shift on a US layout. */
export const SHIFT_CHARACTERS = new Set(["!", "@", "#", "$", "%", "^", "&", "*", "(", ")", "_", "+", "{", "}", "|", ":", '"', "<", ">", "?", "~"]);

/** The number key under each shifted digit-row symbol. */
export const SHIFTED_DIGITS: Record<string, number> = {
  "!": 49, "@": 50, "#": 51, "$": 52, "%": 53, "^": 54, "&": 55, "*": 56, "(": 57, ")": 48,
};

/** The keyCode and Shift state that type one character, or null when there is none. */
export function characterKey(char: string): { keyCode: number; shift: boolean } | null {
  if (SHIFTED_DIGITS[char]) return { keyCode: SHIFTED_DIGITS[char], shift: true };
  if (KEY_CODES[char] && char.length === 1) return { keyCode: KEY_CODES[char], shift: SHIFT_CHARACTERS.has(char) };
  if (/^[a-zA-Z]$/.test(char)) return { keyCode: char.toUpperCase().charCodeAt(0), shift: char !== char.toLowerCase() };
  if (/^[0-9]$/.test(char)) return { keyCode: char.charCodeAt(0), shift: false };
  if (char === "\n" || char === "\r") return { keyCode: 13, shift: false };
  if (char === "\t") return { keyCode: 9, shift: false };
  return null;
}
