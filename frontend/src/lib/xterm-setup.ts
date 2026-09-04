/**
 * Shared xterm.js factory for every terminal/console surface.
 *
 * One place defines the theme, addons, renderer, and resize handling so the
 * three consumers (floating terminal, console text tab, full-page console)
 * cannot drift apart. The renderer is GPU-accelerated (WebGL) with an automatic
 * fall back to xterm's DOM renderer when WebGL2 is unavailable (headless, locked
 * down VDI, lost GPU context) so it degrades cleanly instead of failing.
 */

import { Terminal, ITheme } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { SearchAddon } from "@xterm/addon-search";
import { WebglAddon } from "@xterm/addon-webgl";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import "@xterm/xterm/css/xterm.css";

// Tokyo Night palette shared by all terminals.
/**
 * Chrome surfaces. important: these are the hex twin of the --terminal-* tokens in
 * src/app/globals.css. xterm paints into a canvas and takes hex strings, so it cannot resolve a
 * CSS variable the way the surrounding Tailwind chrome does, which is why the palette exists in
 * both places. Change one and change the other.
 */
export const TERMINAL_SURFACE = {
  bg: "#14111c",
  deep: "#100e16",
  raised: "#1f1a2b",
  border: "#2e2740",
} as const;

export const ORCASTRA_TERMINAL_THEME: ITheme = {
  background: TERMINAL_SURFACE.bg,
  foreground: "#a9b1d6",
  cursor: "#c0caf5",
  cursorAccent: TERMINAL_SURFACE.bg,
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

const TERMINAL_FONT =
  '"JetBrains Mono", "Fira Code", "Cascadia Code", Consolas, monospace';

/** Default terminal font size (px). Zoom % scales relative to this. */
export const DEFAULT_FONT_SIZE = 14;
const MIN_FONT_SIZE = 8;
const MAX_FONT_SIZE = 32;

export interface CreateXtermOptions {
  fontSize?: number; // default 14
  scrollback?: number; // default 10000 (memory-bounded; WebGL render cost is viewport-only)
}

export interface XtermBundle {
  term: Terminal;
  fitAddon: FitAddon;
  searchAddon: SearchAddon;
  /** rAF-coalesced fit - a burst of layout changes triggers a single fit(). */
  fit: () => void;
  /** Set the font size (clamped) and re-fit. This is how zoom works: changing the
   *  cell size keeps WebGL text pixel-crisp, unlike CSS zoom/scale which reflows and
   *  blurs. Returns the size actually applied. */
  setFontSize: (px: number) => number;
  /** Disposes the renderer addon (if any) and the terminal, and cancels a pending fit. */
  dispose: () => void;
}

// note: xterm's WebGL addon needs a WebGL2 context. Constructing it can throw in
// sandboxed/headless environments, so we feature-detect first and wrap in try/catch.
// On failure we return null and xterm uses its built-in DOM renderer.
//
// An intermediate Canvas renderer (faster than DOM) would normally sit between WebGL
// and DOM, but @xterm/addon-canvas has no release compatible with @xterm/xterm v6
// (every published version pins its peer dependency to xterm ^5), so wiring it in
// would force a broken peer-dep resolution across the whole tree. If upstream ships a
// v6-compatible canvas addon, add a tryLoadCanvas() branch here between WebGL and DOM.
function selectRenderer(term: Terminal): WebglAddon | null {
  try {
    const probe = document.createElement("canvas").getContext("webgl2");
    if (!probe) return null;
  } catch {
    return null;
  }
  try {
    const addon = new WebglAddon();
    // On GPU context loss, drop the addon; xterm transparently reverts to DOM.
    addon.onContextLoss(() => {
      try {
        addon.dispose();
      } catch {
        /* already disposed */
      }
    });
    term.loadAddon(addon);
    return addon;
  } catch {
    return null;
  }
}

export function createXterm(
  container: HTMLElement,
  opts: CreateXtermOptions = {},
): XtermBundle {
  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: "block",
    fontSize: opts.fontSize ?? DEFAULT_FONT_SIZE,
    fontFamily: TERMINAL_FONT,
    theme: ORCASTRA_TERMINAL_THEME,
    allowProposedApi: true,
    scrollback: opts.scrollback ?? 10000,
    convertEol: true,
  });

  const fitAddon = new FitAddon();
  const searchAddon = new SearchAddon();
  const unicode11Addon = new Unicode11Addon();
  term.loadAddon(fitAddon);
  term.loadAddon(new WebLinksAddon());
  term.loadAddon(searchAddon);
  term.loadAddon(unicode11Addon);
  // Use Unicode 11 width tables so emoji and wide CJK glyphs occupy the correct
  // number of cells - this is the rendering half of the verbatim UTF-8 byte path.
  term.unicode.activeVersion = "11";

  term.open(container);

  // Renderer must be attached after open(); falls back to DOM on failure.
  const renderer = selectRenderer(term);

  let rafId: number | null = null;
  const fit = () => {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      try {
        fitAddon.fit();
      } catch {
        /* container detached mid-resize */
      }
    });
  };

  const setFontSize = (px: number): number => {
    const clamped = Math.max(MIN_FONT_SIZE, Math.min(MAX_FONT_SIZE, Math.round(px)));
    if (term.options.fontSize !== clamped) {
      term.options.fontSize = clamped;
      fit();
    }
    return clamped;
  };

  const dispose = () => {
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    try {
      renderer?.dispose();
    } catch {
      /* noop */
    }
    try {
      term.dispose();
    } catch {
      /* noop */
    }
  };

  fit();
  return { term, fitAddon, searchAddon, fit, setFontSize, dispose };
}
