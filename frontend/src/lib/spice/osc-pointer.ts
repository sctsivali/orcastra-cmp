/**
 * On-screen-cursor pointer helpers - dispatch synthetic mouse/wheel events onto
 * the SPICE canvas, which the vendored spice-html5 input layer reads as real
 * pointer input. Pure DOM helpers (no React) so the component keeps state + JSX.
 *
 * Mouse-mode aware:
 * - CLIENT mode (guest runs spice-vdagent): the guest accepts ABSOLUTE positions,
 *   so a click lands exactly under the on-screen crosshair.
 * - SERVER mode (no agent): the guest only accepts RELATIVE motion, so the cursor
 *   behaves like a trackpad - dragging moves the guest pointer, a tap clicks where
 *   the guest pointer currently is. Absolute "click at this pixel" is impossible
 *   without the agent, so we never pretend otherwise.
 */

export type MouseMode = "client" | "server" | "unknown";

// Click micro-timing: the guest needs a beat between move/press/release to
// register the transition. Matches the values the component used historically.
const PRESS_DELAY_MS = 20;
const RELEASE_DELAY_MS = 30;
const DOUBLE_GAP_MS = 60;

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function getCanvas(screenId: string): HTMLCanvasElement | null {
  const screen = document.getElementById(screenId);
  return screen ? screen.querySelector("canvas") : null;
}

/** The live SpiceMainConn iff the inputs channel is ready, else null. */
function readyConn(canvas: HTMLCanvasElement | null): any | null {
  const sc = canvas ? (canvas as any).sc : null;
  return sc && sc.inputs && sc.inputs.state === "ready" ? sc : null;
}

export function isReady(canvas: HTMLCanvasElement | null): boolean {
  return !!readyConn(canvas);
}

export function getMouseMode(canvas: HTMLCanvasElement | null): MouseMode {
  const sc = canvas ? (canvas as any).sc : null;
  if (!sc) return "unknown";
  const w = window as any;
  if (sc.mouse_mode === w.SPICE_MOUSE_MODE_SERVER) return "server";
  if (sc.mouse_mode === w.SPICE_MOUSE_MODE_CLIENT) return "client";
  return "unknown";
}

function inBounds(canvas: HTMLCanvasElement, x: number, y: number): boolean {
  const r = canvas.getBoundingClientRect();
  return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
}

function clampToCanvas(canvas: HTMLCanvasElement, x: number, y: number) {
  const r = canvas.getBoundingClientRect();
  return { x: Math.max(r.left, Math.min(r.right, x)), y: Math.max(r.top, Math.min(r.bottom, y)) };
}

function fire(canvas: HTMLCanvasElement, type: string, init: MouseEventInit) {
  canvas.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, ...init }));
}

/**
 * Prime spice-html5's last-known pointer position so the NEXT server-mode move
 * emits a near-zero delta instead of a jump. No-op (and harmless) in client mode.
 * Call at the start of a server-mode drag.
 */
export function primeServerPosition(canvas: HTMLCanvasElement | null, clientX: number, clientY: number): void {
  const sc = readyConn(canvas);
  if (!sc || !canvas) return;
  const r = canvas.getBoundingClientRect();
  sc.mousex = Math.round(((clientX - r.left) * canvas.width) / r.width);
  sc.mousey = Math.round(((clientY - r.top) * canvas.height) / r.height);
}

/** Dispatch a move. `buttons` is the held-button bitmask (1=left, 2=right). */
export function sendMove(canvas: HTMLCanvasElement | null, clientX: number, clientY: number, buttons = 0): void {
  if (!readyConn(canvas) || !canvas) return;
  const { x, y } = clampToCanvas(canvas, clientX, clientY);
  fire(canvas, "mousemove", { clientX: x, clientY: y, button: 0, buttons });
}

const btnIndex = (button: "left" | "right") => (button === "left" ? 0 : 2);
const btnMask = (button: "left" | "right") => (button === "left" ? 1 : 2);

/** Press a button at a point (positions first). Returns false if not ready / out of bounds in client mode. */
export function sendDown(canvas: HTMLCanvasElement | null, clientX: number, clientY: number, button: "left" | "right", mode: MouseMode): boolean {
  if (!readyConn(canvas) || !canvas) return false;
  // In client mode the press must land inside the canvas; in server mode position
  // is irrelevant (relative), so allow it.
  if (mode !== "server" && !inBounds(canvas, clientX, clientY)) return false;
  if (mode !== "server") fire(canvas, "mousemove", { clientX, clientY, button: 0, buttons: 0 });
  fire(canvas, "mousedown", { clientX, clientY, button: btnIndex(button), buttons: btnMask(button) });
  return true;
}

export function sendUp(canvas: HTMLCanvasElement | null, clientX: number, clientY: number, button: "left" | "right"): void {
  if (!readyConn(canvas) || !canvas) return;
  fire(canvas, "mouseup", { clientX, clientY, button: btnIndex(button), buttons: 0 });
}

/**
 * Full click (down -> up, plus contextmenu for right). In client mode it clicks at
 * (clientX, clientY); in server mode it clicks wherever the guest pointer is (the
 * coordinates only seed the synthetic event, the guest ignores absolute position).
 */
export async function sendClick(canvas: HTMLCanvasElement | null, clientX: number, clientY: number, button: "left" | "right", mode: MouseMode): Promise<void> {
  if (!readyConn(canvas) || !canvas) return;
  if (mode !== "server") {
    if (!inBounds(canvas, clientX, clientY)) return;
    fire(canvas, "mousemove", { clientX, clientY, button: 0, buttons: 0 });
  }
  await delay(PRESS_DELAY_MS);
  fire(canvas, "mousedown", { clientX, clientY, button: btnIndex(button), buttons: btnMask(button) });
  await delay(RELEASE_DELAY_MS);
  fire(canvas, "mouseup", { clientX, clientY, button: btnIndex(button), buttons: 0 });
  if (button === "right") fire(canvas, "contextmenu", { clientX, clientY, button: 2 });
}

/** Double left-click (two press/release pairs in quick succession). */
export async function sendDoubleClick(canvas: HTMLCanvasElement | null, clientX: number, clientY: number, mode: MouseMode): Promise<void> {
  await sendClick(canvas, clientX, clientY, "left", mode);
  await delay(DOUBLE_GAP_MS);
  await sendClick(canvas, clientX, clientY, "left", mode);
}

/**
 * Scroll one notch. spice-html5's handle_mousewheel only reads deltaY sign, so a
 * fixed magnitude is fine. Position seeds the event (used in client mode).
 */
export function sendScroll(canvas: HTMLCanvasElement | null, clientX: number, clientY: number, direction: "up" | "down"): void {
  if (!readyConn(canvas) || !canvas) return;
  const { x, y } = clampToCanvas(canvas, clientX, clientY);
  canvas.dispatchEvent(new WheelEvent("wheel", {
    bubbles: true, cancelable: true, view: window,
    clientX: x, clientY: y, deltaY: direction === "up" ? -120 : 120,
  }));
}
