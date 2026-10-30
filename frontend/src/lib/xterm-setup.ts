/**
 * One xterm factory for every terminal surface: the console's Text and Serial tabs, the floating
 * terminal and both full pages. They used to carry four copies of the setup, which drifted.
 *
 * - Renderer: WebGL, falling back to xterm's DOM renderer when WebGL2 is missing or its context is
 *   lost. Heavy output (logs, `top`) costs a fraction of the main-thread time it did on the DOM.
 * - Unicode 11 cell widths, so emoji and wide CJK glyphs take the cells the program expects.
 * - `convertEol` stays off: the PTY already turns LF into CRLF, and translating again moved the
 *   cursor to column 0 on every line feed a full-screen program (vim, htop, less) used to move
 *   down a row.
 * - Zoom changes the font size, so glyphs stay crisp and the grid refits (CSS zoom blurred them
 *   and put the mouse selection off by the zoom factor).
 * - The font is Geist Mono, the product's monospace face, already loaded through next/font.
 */

import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon } from "@xterm/addon-search";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import { GeistMono } from "geist/font/mono";
import "@xterm/xterm/css/xterm.css";
import { TERMINAL_THEME } from "@/lib/terminal-theme";

export const DEFAULT_FONT_SIZE = 14;
const MIN_FONT_SIZE = 8;
const MAX_FONT_SIZE = 32;
export const TERMINAL_FONT = `${GeistMono.style.fontFamily}, ui-monospace, monospace`;

export interface XtermBundle {
  term: Terminal;
  searchAddon: SearchAddon;
  /** rAF-coalesced fit: a burst of layout changes refits once. */
  fit: () => void;
  /** Set the font size (clamped) and refit; returns the size applied. */
  setFontSize: (px: number) => number;
  dispose: () => void;
}

/** Waits (briefly) for the terminal font, so xterm measures its cells with the real glyphs. */
export async function terminalFontReady(size = DEFAULT_FONT_SIZE): Promise<void> {
  if (typeof document === "undefined" || !document.fonts?.load) return;
  await Promise.race([
    document.fonts.load(`${size}px ${GeistMono.style.fontFamily}`).catch(() => undefined),
    new Promise((resolve) => setTimeout(resolve, 1500)),
  ]);
}

function attachRenderer(term: Terminal): WebglAddon | null {
  try {
    if (!document.createElement("canvas").getContext("webgl2")) return null;
  } catch {
    return null;
  }
  try {
    const addon = new WebglAddon();
    // On a lost GPU context xterm falls back to its DOM renderer once the addon is gone.
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

export function createXterm(container: HTMLElement, options: { fontSize?: number; scrollback?: number } = {}): XtermBundle {
  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: "block",
    cursorInactiveStyle: "outline",
    fontSize: options.fontSize ?? DEFAULT_FONT_SIZE,
    fontFamily: TERMINAL_FONT,
    theme: TERMINAL_THEME,
    scrollback: options.scrollback ?? 10000,
    allowProposedApi: true,
    convertEol: false,
    macOptionIsMeta: true,
    rightClickSelectsWord: true,
  });
  const fitAddon = new FitAddon();
  const searchAddon = new SearchAddon();
  term.loadAddon(fitAddon);
  term.loadAddon(searchAddon);
  term.loadAddon(new WebLinksAddon());
  term.loadAddon(new Unicode11Addon());
  term.unicode.activeVersion = "11";
  term.open(container);
  const renderer = attachRenderer(term);
  // note: with the WebGL renderer the text is not in the DOM; this lets the QA harness read the
  // buffer. Not enumerable, so nothing that walks the element picks it up.
  Object.defineProperty(container, "__xterm", { value: term, configurable: true });

  let frame: number | null = null;
  const fit = () => {
    if (frame !== null) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      // A hidden container measures 0x0, and fitting to it would collapse the grid.
      if (!container.clientWidth || !container.clientHeight) return;
      try {
        fitAddon.fit();
      } catch {
        /* detached mid-resize */
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
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
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
  return { term, searchAddon, fit, setFontSize, dispose };
}
