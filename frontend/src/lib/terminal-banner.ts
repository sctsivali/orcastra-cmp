/**
 * The box at the start of a text session, and the rule for anything else this app writes to xterm.
 *
 * important: the box is a DOM element over four blank rows, not box characters in the stream. It
 * used to be characters, and it went crooked three ways: its width came from string length, so a
 * long name pushed the right border out; a window under 50 columns wrapped it; and xterm re-wraps
 * old lines when a window narrows, which no width calculation can undo. CSS lays this one out, and
 * a blank row has nothing to re-wrap. The cost is that its text is not in the buffer, so select-all
 * copies blank rows there and search does not find it.
 */

import type { IDisposable, Terminal } from "@xterm/xterm";
import { TERMINAL_THEME } from "@/lib/terminal-theme";

const BOX_ROWS = 4;
// Under this the box is most of the screen, and a shell's first lines would scroll it off at once.
const MIN_ROWS = 8;

// C0, DEL and C1, then the format characters that reorder what a reader sees.
// eslint-disable-next-line no-control-regex
const UNSAFE = /[\u0000-\u001f\u007f-\u009f\u061c\u200e-\u200f\u2028-\u2029\u202a-\u202e\u2066-\u2069]/g;

/**
 * Text that did not come from the PTY, made safe to write to the terminal.
 *
 * important: xterm executes whatever it is given. An instance name comes from the URL and the
 * server echoes it in a refusal, so without this a crafted link can clear the screen, paint a fake
 * prompt or plant an OSC 8 hyperlink under an operator's session.
 */
export function terminalSafe(text: string): string {
  return text.replace(UNSAFE, "");
}

function textLine(text: string): HTMLDivElement {
  const line = document.createElement("div");
  line.textContent = text;
  // note: CSS clips, so the whole name stays in the DOM; the page header shows it in full.
  line.style.cssText = "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0";
  return line;
}

function buildBox(title: string, detail: string): HTMLDivElement {
  const box = document.createElement("div");
  box.dataset.slot = "terminal-connect-box";
  // important: anchored to the bottom of the decoration, which sits on the LAST reserved row. xterm
  // hides a decoration as soon as its own row leaves the viewport, so on the first row one scrolled
  // line would take the whole box away.
  box.style.cssText =
    "position:absolute;left:0;bottom:0;height:400%;width:fit-content;max-width:100%;" +
    "box-sizing:border-box;display:flex;flex-direction:column;justify-content:center;padding:0 1ch";
  box.style.border = `1px solid ${TERMINAL_THEME.blue}`;
  box.style.color = String(TERMINAL_THEME.foreground);
  const heading = textLine(title);
  heading.style.color = String(TERMINAL_THEME.cyan);
  heading.style.fontWeight = "700";
  box.append(heading, textLine(detail));
  return box;
}

/** Draws the box on the rows the cursor is on. Call once, before anything else is written. */
export function drawConnectBox(term: Terminal, title: string, detail: string): IDisposable {
  const heading = terminalSafe(title);
  const line = terminalSafe(detail);
  const plain = () => term.write(`\x1b[1;36m${heading}\x1b[0m\r\n${line}\r\n`);

  if (term.rows < MIN_ROWS) {
    plain();
    return { dispose: () => {} };
  }

  const subscriptions: IDisposable[] = [];
  let closed = false;
  // note: a marker taken right after write() lands on the old row, because writes are asynchronous.
  term.write("\r\n".repeat(BOX_ROWS), () => {
    if (closed) return;
    const marker = term.registerMarker(-1);
    const decoration = term.registerDecoration({ marker, width: 1, height: 1, layer: "top" });
    if (!decoration) {
      plain();
      return;
    }
    subscriptions.push(decoration);
    let box: HTMLDivElement | null = null;
    subscriptions.push(
      decoration.onRender((element) => {
        // note: xterm puts the option width back on every refresh, so this has to run each time.
        element.style.width = "100%";
        element.style.pointerEvents = "none";
        if (!box) {
          box = buildBox(heading, line);
          element.append(box);
        }
        box.style.fontFamily = String(term.options.fontFamily);
        box.style.fontSize = `${term.options.fontSize}px`;
      }),
    );

    // A program can write over these rows: `clear` on a serial console homes the cursor and erases,
    // GRUB and `tput cup` paint where they like. The box would then sit on top of their text.
    const watch = term.onWriteParsed(() => {
      const buffer = term.buffer.active;
      if (marker.isDisposed) {
        watch.dispose();
        return;
      }
      if (buffer.type !== "normal") return;
      for (let row = marker.line - BOX_ROWS + 1; row <= marker.line; row += 1) {
        if (buffer.getLine(row)?.translateToString(true)) {
          decoration.dispose();
          watch.dispose();
          return;
        }
      }
      // In the scrollback no program can reach it, so stop reading these rows on every write.
      if (marker.line < buffer.baseY) watch.dispose();
    });
    subscriptions.push(watch);
  });

  return {
    dispose: () => {
      closed = true;
      for (const subscription of subscriptions) subscription.dispose();
    },
  };
}
