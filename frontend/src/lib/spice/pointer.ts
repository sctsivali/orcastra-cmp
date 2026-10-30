/**
 * The graphical console's pointer: motion, buttons and wheel to the SPICE inputs channel.
 *
 * important: motion is coalesced, never dropped. SPICE lets a client have 8 unacknowledged moves
 * in flight; the vendored handler discarded every move past that, so on a link slower than about
 * 130 ms the last position never reached the guest and a click landed where the pointer had been.
 * Here the newest position (or, in server mode, the summed movement) waits and goes out as soon
 * as an ACK opens the window, and a press or release always sends the pending position first.
 *
 * Client mouse mode (an absolute device in the guest) sends positions. Server mode (relative,
 * typically a BIOS or a guest without a tablet driver) locks the browser pointer on click and
 * sends movement, which is the only way relative motion stays aligned.
 */

type SpiceGlobals = {
  SpiceMiniData: new () => { build_msg: (type: number, body: unknown) => void };
  SpiceMsgcMousePosition: new (sc: unknown) => { x: number; y: number; buttons_state: number; display_id: number };
  SpiceMsgcMouseMotion: new (sc: unknown) => { x: number; y: number; buttons_state: number; display_id: number };
  SpiceMsgcMousePress: new () => { button: number; buttons_state: number };
  SpiceMsgcMouseRelease: new () => { button: number; buttons_state: number };
  SPICE_MSGC_INPUTS_MOUSE_POSITION: number;
  SPICE_MSGC_INPUTS_MOUSE_MOTION: number;
  SPICE_MSGC_INPUTS_MOUSE_PRESS: number;
  SPICE_MSGC_INPUTS_MOUSE_RELEASE: number;
  SPICE_MOUSE_MODE_CLIENT: number;
  SPICE_INPUT_MOTION_ACK_BUNCH: number;
};

export interface SpiceInputs {
  state?: string;
  waiting_for_ack: number;
  send_msg: (msg: unknown) => void;
  onmotionack?: () => void;
}

export interface SpiceMain {
  inputs?: SpiceInputs;
  mouse_mode?: number;
}

export type MouseMode = "client" | "server";

const WHEEL_UP = 4;
const WHEEL_DOWN = 5;
// One wheel notch per 100 px of pixel-mode scroll, about one detent of a desktop mouse.
const WHEEL_NOTCH_PX = 100;
const LINE_PX = 40;
const PAGE_PX = 800;

function g(): SpiceGlobals {
  return window as unknown as SpiceGlobals;
}

export class PointerBridge {
  private readonly canvas: HTMLCanvasElement;
  private readonly sc: SpiceMain;
  private mode: MouseMode = "client";
  private buttons = 0;
  private pending: { x: number; y: number } | null = null;
  private pendingDelta = { dx: 0, dy: 0 };
  private wheelCarry = 0;
  private sentAt: number[] = [];
  private readonly onRtt: (ms: number) => void;
  private readonly onLockChange: (locked: boolean) => void;

  constructor(
    canvas: HTMLCanvasElement,
    sc: SpiceMain,
    options: { onRtt?: (ms: number) => void; onLockChange?: (locked: boolean) => void } = {},
  ) {
    this.canvas = canvas;
    this.sc = sc;
    this.onRtt = options.onRtt ?? (() => {});
    this.onLockChange = options.onLockChange ?? (() => {});
    canvas.addEventListener("pointermove", this.onMove);
    canvas.addEventListener("pointerdown", this.onDown);
    canvas.addEventListener("pointerup", this.onUp);
    canvas.addEventListener("pointercancel", this.onCancel);
    canvas.addEventListener("wheel", this.onWheel, { passive: false });
    canvas.addEventListener("contextmenu", this.onContextMenu);
    document.addEventListener("pointerlockchange", this.onPointerLockChange);
    this.attachAck();
  }

  dispose(): void {
    this.canvas.removeEventListener("pointermove", this.onMove);
    this.canvas.removeEventListener("pointerdown", this.onDown);
    this.canvas.removeEventListener("pointerup", this.onUp);
    this.canvas.removeEventListener("pointercancel", this.onCancel);
    this.canvas.removeEventListener("wheel", this.onWheel);
    this.canvas.removeEventListener("contextmenu", this.onContextMenu);
    document.removeEventListener("pointerlockchange", this.onPointerLockChange);
    if (document.pointerLockElement === this.canvas) document.exitPointerLock();
    if (this.sc.inputs && this.sc.inputs.onmotionack === this.onAck) this.sc.inputs.onmotionack = undefined;
  }

  /** The inputs channel can appear after the canvas; call again once it does. */
  attachAck(): void {
    if (this.sc.inputs) this.sc.inputs.onmotionack = this.onAck;
  }

  /** An ACK freed room in the window: sample the round trip, then send what waited. */
  private onAck = (): void => {
    this.sampleAck();
    this.flush();
  };

  setMode(mode: MouseMode): void {
    if (this.mode === mode) return;
    this.mode = mode;
    this.pending = null;
    this.pendingDelta = { dx: 0, dy: 0 };
    if (mode === "client" && document.pointerLockElement === this.canvas) document.exitPointerLock();
  }

  get mouseMode(): MouseMode {
    return this.mode;
  }

  get locked(): boolean {
    return document.pointerLockElement === this.canvas;
  }

  /** Send a position in guest pixels (the on-screen cursor uses this too). */
  moveTo(x: number, y: number): void {
    this.pending = { x: Math.max(0, Math.round(x)), y: Math.max(0, Math.round(y)) };
    this.flush();
  }

  click(button = 0): void {
    this.press(button);
    this.release(button);
  }

  press(button: number): void {
    const spiceButton = button + 1;
    if (spiceButton < 1 || spiceButton > 3) return;
    this.flush(true);
    this.buttons |= 1 << button;
    this.sendButton(g().SPICE_MSGC_INPUTS_MOUSE_PRESS, spiceButton);
  }

  release(button: number): void {
    const spiceButton = button + 1;
    if (spiceButton < 1 || spiceButton > 3) return;
    this.flush(true);
    this.buttons &= ~(1 << button);
    this.sendButton(g().SPICE_MSGC_INPUTS_MOUSE_RELEASE, spiceButton);
  }

  /** Let go of every button, for a blur or a pointer that left mid-drag. */
  releaseButtons(): void {
    for (const button of [0, 1, 2]) {
      if (this.buttons & (1 << button)) this.release(button);
    }
  }

  private ready(): SpiceInputs | null {
    const inputs = this.sc.inputs;
    return inputs && inputs.state === "ready" ? inputs : null;
  }

  private toGuest(event: { clientX: number; clientY: number }): { x: number; y: number } {
    const rect = this.canvas.getBoundingClientRect();
    const sx = rect.width ? this.canvas.width / rect.width : 1;
    const sy = rect.height ? this.canvas.height / rect.height : 1;
    return {
      x: Math.min(this.canvas.width - 1, Math.max(0, Math.round((event.clientX - rect.left) * sx))),
      y: Math.min(this.canvas.height - 1, Math.max(0, Math.round((event.clientY - rect.top) * sy))),
    };
  }

  /** Sends what is pending if the ACK window allows it, or regardless when `force` is set. */
  private flush = (force = false): void => {
    const inputs = this.ready();
    if (!inputs) return;
    const bunch = g().SPICE_INPUT_MOTION_ACK_BUNCH || 4;
    if (!force && inputs.waiting_for_ack >= 2 * bunch) return;
    const G = g();
    if (this.mode === "client") {
      if (!this.pending) return;
      const body = new G.SpiceMsgcMousePosition(this.sc);
      body.x = this.pending.x;
      body.y = this.pending.y;
      body.buttons_state = this.buttons;
      body.display_id = 0;
      this.pending = null;
      this.send(inputs, G.SPICE_MSGC_INPUTS_MOUSE_POSITION, body);
    } else {
      const { dx, dy } = this.pendingDelta;
      if (!dx && !dy) return;
      const body = new G.SpiceMsgcMouseMotion(this.sc);
      body.x = dx;
      body.y = dy;
      body.buttons_state = this.buttons;
      body.display_id = 0;
      this.pendingDelta = { dx: 0, dy: 0 };
      this.send(inputs, G.SPICE_MSGC_INPUTS_MOUSE_MOTION, body);
    }
  };

  private send(inputs: SpiceInputs, type: number, body: unknown): void {
    const msg = new (g().SpiceMiniData)();
    msg.build_msg(type, body);
    inputs.send_msg(msg);
    inputs.waiting_for_ack += 1;
    this.sentAt.push(performance.now());
    if (this.sentAt.length > 64) this.sentAt.splice(0, this.sentAt.length - 64);
    if (inputs.onmotionack !== this.onAck) inputs.onmotionack = this.onAck;
  }

  /** The server acknowledges motion a bunch at a time: the round trip of the bunch's last move. */
  private sampleAck(): void {
    const bunch = g().SPICE_INPUT_MOTION_ACK_BUNCH || 4;
    if (this.sentAt.length >= bunch) {
      const sent = this.sentAt[bunch - 1];
      this.sentAt.splice(0, bunch);
      this.onRtt(performance.now() - sent);
    }
  }

  private sendButton(type: number, button: number): void {
    const inputs = this.ready();
    if (!inputs) return;
    const G = g();
    const body = type === G.SPICE_MSGC_INPUTS_MOUSE_PRESS ? new G.SpiceMsgcMousePress() : new G.SpiceMsgcMouseRelease();
    body.button = button;
    body.buttons_state = this.buttons;
    const msg = new G.SpiceMiniData();
    msg.build_msg(type, body);
    inputs.send_msg(msg);
  }

  private onMove = (event: PointerEvent): void => {
    if (this.mode === "client") {
      this.pending = this.toGuest(event);
      this.flush();
      return;
    }
    if (!this.locked) return;
    const rect = this.canvas.getBoundingClientRect();
    const sx = rect.width ? this.canvas.width / rect.width : 1;
    const sy = rect.height ? this.canvas.height / rect.height : 1;
    this.pendingDelta.dx += Math.round(event.movementX * sx);
    this.pendingDelta.dy += Math.round(event.movementY * sy);
    this.flush();
  };

  private onDown = (event: PointerEvent): void => {
    this.canvas.focus({ preventScroll: true });
    if (this.mode === "server" && !this.locked) {
      // The first click captures the pointer; it is not sent to the guest.
      event.preventDefault();
      void this.canvas.requestPointerLock?.();
      return;
    }
    if (event.button > 2) return; // back/forward are not guest buttons
    event.preventDefault();
    if (event.pointerType !== "touch") {
      try {
        this.canvas.setPointerCapture(event.pointerId);
      } catch {
        /* the pointer is already gone */
      }
    }
    if (this.mode === "client") this.pending = this.toGuest(event);
    this.press(event.button);
  };

  private onUp = (event: PointerEvent): void => {
    if (event.button > 2) return;
    event.preventDefault();
    if (this.mode === "client") this.pending = this.toGuest(event);
    if (this.buttons & (1 << event.button)) this.release(event.button);
    if (this.canvas.hasPointerCapture?.(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
  };

  private onCancel = (): void => {
    this.releaseButtons();
  };

  private onWheel = (event: WheelEvent): void => {
    event.preventDefault();
    const unit = event.deltaMode === 1 ? LINE_PX : event.deltaMode === 2 ? PAGE_PX : 1;
    this.wheelCarry += event.deltaY * unit;
    // A discrete wheel detent of less than a notch still scrolls once.
    if (Math.abs(this.wheelCarry) < WHEEL_NOTCH_PX && Math.abs(event.deltaY * unit) >= WHEEL_NOTCH_PX / 2) {
      this.wheelCarry = Math.sign(this.wheelCarry) * WHEEL_NOTCH_PX;
    }
    let notches = Math.trunc(this.wheelCarry / WHEEL_NOTCH_PX);
    if (!notches) return;
    this.wheelCarry -= notches * WHEEL_NOTCH_PX;
    notches = Math.max(-10, Math.min(10, notches));
    if (this.mode === "client") this.pending = this.toGuest(event);
    this.flush(true);
    const G = g();
    const button = notches < 0 ? WHEEL_UP : WHEEL_DOWN;
    for (let i = 0; i < Math.abs(notches); i++) {
      this.sendButton(G.SPICE_MSGC_INPUTS_MOUSE_PRESS, button);
      this.sendButton(G.SPICE_MSGC_INPUTS_MOUSE_RELEASE, button);
    }
  };

  private onContextMenu = (event: Event): void => {
    event.preventDefault();
  };

  private onPointerLockChange = (): void => {
    if (!this.locked) this.releaseButtons();
    this.onLockChange(this.locked);
  };
}
