/**
 * Shared SPICE key tables.
 *
 * spice-html5 maps JavaScript keyCodes to PC scancodes via
 * window.keycode_to_start_scan / keycode_to_end_scan. These tables are the one
 * canonical copy used by every SPICE input path (virtual keyboard, BIOS spam,
 * clipboard paste fallback). US layout - the agent clipboard channel is the
 * layout-independent path for arbitrary text.
 */

// Named key -> JavaScript keyCode.
export const KEY_CODE_MAP: Record<string, number> = {
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
  "-": 189, _: 189, ".": 190, ">": 190, "/": 191, "?": 191,
  "`": 192, "~": 192, "[": 219, "{": 219, "\\": 220, "|": 220,
  "]": 221, "}": 221, "'": 222, '"': 222,
};

// Symbols that require Shift on a US keyboard.
export const SHIFT_SYMBOLS: ReadonlySet<string> = new Set([
  "!", "@", "#", "$", "%", "^", "&", "*", "(", ")", "_", "+",
  "{", "}", "|", ":", '"', "<", ">", "?", "~",
]);

// Number-row symbols -> base digit keyCode (typed with Shift held).
export const NUM_SHIFT_MAP: Record<string, number> = {
  "!": 49, "@": 50, "#": 51, "$": 52, "%": 53,
  "^": 54, "&": 55, "*": 56, "(": 57, ")": 48,
};

export interface KeyStroke {
  keyCode: number;
  shift: boolean;
}

// Full printable-ASCII -> { keyCode, shift } table for paste-by-keystroke.
export const CHAR_TO_KEYSTROKE: Record<string, KeyStroke> = (() => {
  const map: Record<string, KeyStroke> = {
    "0": { keyCode: 48, shift: false }, "1": { keyCode: 49, shift: false },
    "2": { keyCode: 50, shift: false }, "3": { keyCode: 51, shift: false },
    "4": { keyCode: 52, shift: false }, "5": { keyCode: 53, shift: false },
    "6": { keyCode: 54, shift: false }, "7": { keyCode: 55, shift: false },
    "8": { keyCode: 56, shift: false }, "9": { keyCode: 57, shift: false },
    ")": { keyCode: 48, shift: true }, "!": { keyCode: 49, shift: true },
    "@": { keyCode: 50, shift: true }, "#": { keyCode: 51, shift: true },
    $: { keyCode: 52, shift: true }, "%": { keyCode: 53, shift: true },
    "^": { keyCode: 54, shift: true }, "&": { keyCode: 55, shift: true },
    "*": { keyCode: 56, shift: true }, "(": { keyCode: 57, shift: true },
    ";": { keyCode: 186, shift: false }, ":": { keyCode: 186, shift: true },
    "=": { keyCode: 187, shift: false }, "+": { keyCode: 187, shift: true },
    ",": { keyCode: 188, shift: false }, "<": { keyCode: 188, shift: true },
    "-": { keyCode: 189, shift: false }, _: { keyCode: 189, shift: true },
    ".": { keyCode: 190, shift: false }, ">": { keyCode: 190, shift: true },
    "/": { keyCode: 191, shift: false }, "?": { keyCode: 191, shift: true },
    "`": { keyCode: 192, shift: false }, "~": { keyCode: 192, shift: true },
    "[": { keyCode: 219, shift: false }, "{": { keyCode: 219, shift: true },
    "\\": { keyCode: 220, shift: false }, "|": { keyCode: 220, shift: true },
    "]": { keyCode: 221, shift: false }, "}": { keyCode: 221, shift: true },
    "'": { keyCode: 222, shift: false }, '"': { keyCode: 222, shift: true },
    " ": { keyCode: 32, shift: false },
    "\t": { keyCode: 9, shift: false },
    "\n": { keyCode: 13, shift: false },
    "\r": { keyCode: 13, shift: false },
  };
  for (let i = 0; i < 26; i++) {
    const lower = String.fromCharCode(97 + i); // a-z
    const upper = String.fromCharCode(65 + i); // A-Z
    const keyCode = 65 + i; // A=65 ... Z=90
    map[lower] = { keyCode, shift: false };
    map[upper] = { keyCode, shift: true };
  }
  return map;
})();
