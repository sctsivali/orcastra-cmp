import type { ITheme } from "@xterm/xterm";

/**
 * The xterm colour scheme for every terminal and console surface in the app.
 *
 * These hex values mirror the --terminal-* tokens in src/app/globals.css: xterm paints into a
 * canvas and takes hex strings, so it cannot resolve a CSS variable.
 * scripts/checks/check-terminal-theme.mjs parses both files and fails the build if they drift.
 *
 * The chrome (background, cursor, selection) is tinted onto the app's own hue. The ANSI 16 below
 * are unchanged Tokyo Night: an operator reads red as an error and green as a pass, so retinting
 * them would change what output means. On this surface green measures 10.20:1 and the default
 * foreground 8.83:1.
 */

/** Chrome surfaces. Mirrors --terminal-bg / -deep / -raised / -border in globals.css. */
export const TERMINAL_SURFACE = {
  bg: "#14111c",
  deep: "#100e16",
  raised: "#1f1a2b",
  border: "#2e2740",
} as const;

export const TERMINAL_THEME: ITheme = {
  background: TERMINAL_SURFACE.bg,
  foreground: "#a9b1d6",
  cursor: "#c0caf5",
  cursorAccent: TERMINAL_SURFACE.bg,
  // Sits as far off the surface (1.87:1) as Tokyo Night's #33467c does off its own (1.88:1), so
  // selection reads as the same weight of highlight. Selected text measures 6.17:1.
  selectionBackground: "#4b357e",
  selectionForeground: "#c0caf5",
  black: "#32344a",
  red: "#f7768e",
  green: "#9ece6a",
  yellow: "#e0af68",
  blue: "#7aa2f7",
  magenta: "#ad8ee6",
  cyan: "#449dab",
  white: "#787c99",
  brightBlack: "#444b6a",
  brightRed: "#ff7a93",
  brightGreen: "#b9f27c",
  brightYellow: "#ff9e64",
  brightBlue: "#7da6ff",
  brightMagenta: "#bb9af7",
  brightCyan: "#0db9d7",
  brightWhite: "#acb0d0",
};
