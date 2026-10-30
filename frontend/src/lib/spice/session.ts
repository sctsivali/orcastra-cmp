/**
 * One graphical console session: the vendored SpiceMainConn plus everything the console does
 * around it. No React; the component renders what this reports.
 *
 * - "connected" means the guest is on screen (the first frame drawn), not that the main channel
 *   linked, which used to lift the overlay onto a blank canvas.
 * - How a session ends decides what happens next: 1000 (the instance closed it, or another viewer
 *   took the console over) and 4403/4404 are final; a dropped connection (1006, 1011) reconnects
 *   with backoff, at once when the network or the tab comes back.
 * - With the guest agent running, the guest's resolution follows the console's size, and the
 *   clipboard and file drops work both ways. Without it the console scales the picture instead.
 */

import { spiceSocketUrls } from "@/lib/ws-url";
import { loadSpice } from "./loader";
import { KeyboardBridge, downCode, keyForCharacter, upCode, NAMED_KEYS } from "./keyboard";
import { PointerBridge, type MouseMode } from "./pointer";

export type SpiceStatus = "loading" | "connecting" | "connected" | "reconnecting" | "ended" | "error";

export interface SpiceStats {
  /** Motion round trip in ms (smoothed), null until the pointer has moved. */
  rttMs: number | null;
  /** Frames drawn in the last second. */
  fps: number;
  /** Bytes received per second across all channels. */
  bytesPerSecond: number;
}

export interface CursorShape {
  css: string;
  url: string | null;
  width: number;
  height: number;
  hotX: number;
  hotY: number;
}

export interface FileTransferEvent {
  id: number;
  name: string;
  size: number;
  sent: number;
  state: "sending" | "done" | "error" | "cancelled";
  error: string | null;
}

export interface SpiceSessionEvents {
  onStatus?: (status: SpiceStatus, detail?: string) => void;
  onSurface?: (width: number, height: number) => void;
  onStats?: (stats: SpiceStats) => void;
  onMouseMode?: (mode: MouseMode) => void;
  onPointerLock?: (locked: boolean) => void;
  onAgent?: (connected: boolean) => void;
  onCursor?: (shape: CursorShape | null, x: number, y: number) => void;
  onGuestClipboard?: (text: string) => void;
  onFileTransfer?: (event: FileTransferEvent) => void;
}

export interface SpiceUrls {
  main: string;
  channels: string[];
}

type SpiceErrorLike = Error & { code?: number; reason?: string; channel?: string };

// SPICE globals the session reads (loaded from public/spice-html5).
type Globals = {
  SpiceMainConn: new (options: Record<string, unknown>) => SpiceConn;
  SpiceMsgcKeyDown: new () => { code: number };
  SpiceMsgcKeyUp: new () => { code: number };
  SpiceMiniData: new () => { build_msg: (type: number, body: unknown) => void };
  SPICE_MSGC_INPUTS_KEY_DOWN: number;
  SPICE_MSGC_INPUTS_KEY_UP: number;
  SPICE_MOUSE_MODE_CLIENT: number;
};

interface ChannelLike {
  ws?: WebSocket;
  state?: string;
}

export interface SpiceConn extends ChannelLike {
  stop: () => void;
  inputs?: ChannelLike & { send_msg: (m: unknown) => void; waiting_for_ack: number; onmotionack?: () => void };
  display?: ChannelLike;
  cursor?: ChannelLike;
  agent_connected?: boolean;
  mouse_mode?: number;
  draws?: number;
  resize_window?: (flags: number, width: number, height: number, depth: number, x: number, y: number) => void;
  clipboard_grab?: () => void;
  file_xfer_start?: (file: File) => void;
  file_xfer_cancel?: (id: number) => void;
  [hook: string]: unknown;
}

const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000];
const FINAL_CODES = new Set([1000, 1008, 4403, 4404]);
const RESIZE_DEBOUNCE_MS = 350;
const TYPE_INTERVAL_MS = 8;

function globals(): Globals {
  return window as unknown as Globals;
}

function friendly(error: SpiceErrorLike): string {
  if (error.code === 1000) return "The console session ended. Another viewer may have opened this console.";
  if (error.code === 4403) return "Access to this instance was revoked.";
  if (error.reason) return error.reason;
  const message = error.message || "The console connection failed";
  if (/protocol mismatch|refused/i.test(message)) return "The console could not connect. The VM may not be running.";
  if (/timed out/i.test(message)) return "The console did not answer in time.";
  return message;
}

export class SpiceSession {
  private readonly screenId: string;
  private readonly target: { instanceName: string; host: string; project: string };
  private readonly events: SpiceSessionEvents;
  private sc: SpiceConn | null = null;
  private keyboard: KeyboardBridge | null = null;
  private pointer: PointerBridge | null = null;
  private canvas: HTMLCanvasElement | null = null;
  private generation = 0;
  private attempts = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private statsTimer: ReturnType<typeof setInterval> | null = null;
  private resizeTimer: ReturnType<typeof setTimeout> | null = null;
  private status: SpiceStatus = "loading";
  private retryable = false;
  private rtt: number | null = null;
  private lastDraws = 0;
  private bytes = 0;
  private trackedSockets = new WeakSet<WebSocket>();
  private wantedSize: { width: number; height: number } | null = null;
  private resizeGuest = true;
  private hostClipboard = "";
  private cursorShape: CursorShape | null = null;
  private cursorAt = { x: 0, y: 0 };
  private disposed = false;

  constructor(
    screenId: string,
    target: { instanceName: string; host: string; project: string },
    events: SpiceSessionEvents = {},
  ) {
    this.screenId = screenId;
    this.target = target;
    this.events = events;
    window.addEventListener("online", this.onWake);
    document.addEventListener("visibilitychange", this.onVisible);
  }

  // ── lifecycle ────────────────────────────────────────────────────────────────────────────

  async connect(): Promise<void> {
    if (this.disposed) return;
    const generation = ++this.generation;
    this.clearRetry();
    this.teardown();
    // A retry keeps showing "reconnecting" (set when it was scheduled).
    const scriptsReady = Boolean((window as unknown as { SpiceMainConn?: unknown }).SpiceMainConn);
    if (this.attempts === 0) this.setStatus(scriptsReady ? "connecting" : "loading");
    try {
      await loadSpice();
      if (generation !== this.generation) return;
      if (this.attempts === 0 && !scriptsReady) this.setStatus("connecting");
      // One single-use ticket per channel, minted right before the connect.
      const urls = await spiceSocketUrls(this.target.instanceName, this.target.host, this.target.project);
      if (generation !== this.generation) return;
      this.open(urls, generation);
    } catch (error) {
      if (generation !== this.generation) return;
      const status = (error as { status?: number })?.status;
      // A refused ticket (403 forbidden, 409 fenced) will be refused again; a network failure may pass.
      const final = status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429;
      this.fail(error instanceof Error ? error.message : "The console could not connect", !final);
    }
  }

  /** The operator asked to reconnect: start over, counting from the first attempt. */
  reconnect(): void {
    this.attempts = 0;
    void this.connect();
  }

  dispose(): void {
    this.disposed = true;
    this.generation++;
    this.clearRetry();
    this.teardown();
    window.removeEventListener("online", this.onWake);
    document.removeEventListener("visibilitychange", this.onVisible);
  }

  get currentStatus(): SpiceStatus {
    return this.status;
  }

  get connection(): SpiceConn | null {
    return this.sc;
  }

  get surface(): HTMLCanvasElement | null {
    return this.canvas;
  }

  get agentConnected(): boolean {
    return Boolean(this.sc?.agent_connected);
  }

  get mouseMode(): MouseMode {
    return this.pointer?.mouseMode ?? "client";
  }

  private open(urls: SpiceUrls, generation: number): void {
    const G = globals();
    const sc = new G.SpiceMainConn({
      uri: urls.main,
      channel_uris: urls.channels,
      screen_id: this.screenId,
      password: "",
      onerror: (error: SpiceErrorLike) => {
        if (generation !== this.generation) return;
        this.onError(error);
      },
      onsuccess: () => {
        // The main channel linked; "connected" waits for the first frame.
      },
      onagent: () => {
        if (generation !== this.generation) return;
        this.events.onAgent?.(true);
        this.applyGuestSize();
        this.syncHostClipboard();
      },
    });
    this.sc = sc;
    sc.external_input = true;
    sc.oncanvas = (canvas: HTMLCanvasElement) => this.attachCanvas(canvas);
    sc.oncanvasgone = () => this.detachCanvas();
    sc.onfirstdraw = () => {
      if (generation !== this.generation) return;
      this.attempts = 0;
      this.setStatus("connected");
      this.wake();
    };
    sc.onmousemode = (mode: number) => {
      const next: MouseMode = mode === G.SPICE_MOUSE_MODE_CLIENT ? "client" : "server";
      this.pointer?.setMode(next);
      this.events.onMouseMode?.(next);
    };
    sc.onagentdisconnected = () => this.events.onAgent?.(false);
    sc.oncursorshape = (css: string, meta: { url: string; width: number; height: number; hot_x: number; hot_y: number } | null) => {
      this.cursorShape = meta
        ? { css, url: meta.url, width: meta.width, height: meta.height, hotX: meta.hot_x, hotY: meta.hot_y }
        : css === "none" ? null : { css, url: null, width: 0, height: 0, hotX: 0, hotY: 0 };
      this.events.onCursor?.(this.cursorShape, this.cursorAt.x, this.cursorAt.y);
    };
    sc.oncursormove = (x: number, y: number) => {
      this.cursorAt = { x, y };
      this.events.onCursor?.(this.cursorShape, x, y);
    };
    sc.clipboard_on_guest_data = (text: string) => this.onGuestClipboard(text);
    sc.clipboard_get_host_data = () => this.hostClipboard;
    sc.onfiletransfer = (event: FileTransferEvent) => this.events.onFileTransfer?.(event);
    this.startStats();
  }

  private teardown(): void {
    this.stopStats();
    if (this.resizeTimer) clearTimeout(this.resizeTimer);
    this.resizeTimer = null;
    this.detachCanvas();
    const sc = this.sc;
    this.sc = null;
    if (sc) {
      try {
        sc.stop();
      } catch {
        // already closed
      }
    }
  }

  private onError(error: SpiceErrorLike): void {
    const final = error.code !== undefined && FINAL_CODES.has(error.code);
    const message = friendly(error);
    this.teardown();
    if (error.code === 1000) {
      this.setStatus("ended", message);
      return;
    }
    this.fail(message, !final);
  }

  private fail(message: string, retryable: boolean): void {
    this.retryable = retryable;
    if (!retryable || this.attempts >= RECONNECT_DELAYS_MS.length) {
      this.setStatus("error", message);
      return;
    }
    const base = RECONNECT_DELAYS_MS[this.attempts];
    this.attempts += 1;
    this.setStatus("reconnecting", message);
    this.retryTimer = setTimeout(() => void this.connect(), base + Math.floor(Math.random() * 400));
  }

  private clearRetry(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private onWake = (): void => {
    if (this.status === "reconnecting" || (this.status === "error" && this.retryable)) {
      this.attempts = Math.min(this.attempts, 1);
      void this.connect();
    }
  };

  private onVisible = (): void => {
    if (!document.hidden) this.onWake();
  };

  private setStatus(status: SpiceStatus, detail?: string): void {
    this.status = status;
    this.events.onStatus?.(status, detail);
  }

  // ── the canvas and its input ─────────────────────────────────────────────────────────────

  private attachCanvas(canvas: HTMLCanvasElement): void {
    this.detachCanvas();
    this.canvas = canvas;
    canvas.tabIndex = 0;
    canvas.style.outline = "none";
    canvas.style.display = "block";
    canvas.setAttribute("aria-label", "Guest display. Click to send keyboard and mouse to the guest.");
    canvas.setAttribute("role", "application");
    const sc = this.sc;
    if (!sc) return;
    this.keyboard = new KeyboardBridge(canvas, (code, down) => this.sendScancode(code, down));
    this.pointer = new PointerBridge(canvas, sc as never, {
      onRtt: (ms) => {
        this.rtt = this.rtt === null ? ms : this.rtt * 0.7 + ms * 0.3;
      },
      onLockChange: (locked) => this.events.onPointerLock?.(locked),
    });
    const G = globals();
    this.pointer.setMode(sc.mouse_mode === G.SPICE_MOUSE_MODE_CLIENT || sc.mouse_mode === undefined ? "client" : "server");
    canvas.addEventListener("focus", this.syncHostClipboard);
    this.events.onSurface?.(canvas.width, canvas.height);
    this.applyGuestSize();
  }

  private detachCanvas(): void {
    this.keyboard?.dispose();
    this.pointer?.dispose();
    this.keyboard = null;
    this.pointer = null;
    this.canvas?.removeEventListener("focus", this.syncHostClipboard);
    this.canvas = null;
  }

  focus(): void {
    this.canvas?.focus({ preventScroll: true });
  }

  private inputsReady(): boolean {
    const inputs = this.sc?.inputs;
    if (inputs && !inputs.onmotionack) this.pointer?.attachAck();
    return Boolean(inputs && inputs.state === "ready");
  }

  private sendScancode(code: number, down: boolean): boolean {
    if (!this.inputsReady()) return false;
    const G = globals();
    const body = down ? new G.SpiceMsgcKeyDown() : new G.SpiceMsgcKeyUp();
    body.code = code;
    const msg = new G.SpiceMiniData();
    msg.build_msg(down ? G.SPICE_MSGC_INPUTS_KEY_DOWN : G.SPICE_MSGC_INPUTS_KEY_UP, body);
    this.sc!.inputs!.send_msg(msg);
    return true;
  }

  /** Press and release one key by KeyboardEvent.code, with optional modifiers held around it. */
  sendKey(code: string, modifiers: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean } = {}): boolean {
    const mods = [
      modifiers.ctrl && "ControlLeft",
      modifiers.alt && "AltLeft",
      modifiers.shift && "ShiftLeft",
      modifiers.meta && "MetaLeft",
    ].filter(Boolean) as string[];
    const down = downCode(code);
    const up = upCode(code);
    if (down === null || up === null || !this.inputsReady()) return false;
    for (const mod of mods) this.sendScancode(downCode(mod)!, true);
    this.sendScancode(down, true);
    this.sendScancode(up, false);
    for (const mod of mods.reverse()) this.sendScancode(upCode(mod)!, false);
    return true;
  }

  /** A named key ("F2", "Delete") or one character, as the on-screen keyboard sends them. */
  sendNamedOrCharacter(key: string, modifiers: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean } = {}): boolean {
    if (NAMED_KEYS[key]) return this.sendKey(NAMED_KEYS[key], modifiers);
    const typed = keyForCharacter(key);
    if (!typed) return false;
    return this.sendKey(typed.code, { ...modifiers, shift: Boolean(modifiers.shift || typed.shift) });
  }

  /**
   * Types `text` as keystrokes (US layout), for a guest without the agent or a field that does
   * not take a clipboard paste. Returns how many characters had no key and were skipped.
   */
  async typeText(text: string, options: { signal?: AbortSignal; onProgress?: (done: number, total: number) => void } = {}): Promise<number> {
    let skipped = 0;
    const chars = Array.from(text.replace(/\r\n/g, "\n"));
    for (let i = 0; i < chars.length; i++) {
      if (options.signal?.aborted) break;
      const typed = keyForCharacter(chars[i]);
      if (!typed) {
        skipped += 1;
        continue;
      }
      if (!this.sendKey(typed.code, { shift: typed.shift })) break;
      options.onProgress?.(i + 1, chars.length);
      if (i < chars.length - 1) await new Promise((resolve) => setTimeout(resolve, TYPE_INTERVAL_MS));
    }
    return skipped;
  }

  // ── what the guest shows, and its size ───────────────────────────────────────────────────

  /** Ask the guest (through its agent) to match `width` x `height`; ignored without an agent. */
  requestGuestSize(width: number, height: number, enabled: boolean): void {
    this.resizeGuest = enabled;
    this.wantedSize = { width, height };
    if (this.resizeTimer) clearTimeout(this.resizeTimer);
    this.resizeTimer = setTimeout(() => this.applyGuestSize(), RESIZE_DEBOUNCE_MS);
  }

  private applyGuestSize(): void {
    const sc = this.sc;
    const want = this.wantedSize;
    if (!sc || !want || !this.resizeGuest || !sc.agent_connected || !sc.resize_window) return;
    // Multiples of 8 keep the guest's framebuffer stride simple; tiny sizes are not useful.
    const width = Math.max(640, Math.min(4096, Math.floor(want.width / 8) * 8));
    const height = Math.max(480, Math.min(2160, Math.floor(want.height / 8) * 8));
    if (this.canvas && Math.abs(this.canvas.width - width) < 8 && Math.abs(this.canvas.height - height) < 8) return;
    try {
      sc.resize_window(0, width, height, 32, 0, 0);
    } catch {
      // the agent went away; the picture is scaled instead
    }
  }

  /**
   * A guest that blanked its display (idle, or a parked framebuffer QEMU shows as "Display
   * output is not active") wakes on input. A lone Shift tap does nothing else anywhere.
   */
  private wake(): void {
    window.setTimeout(() => {
      if (this.status !== "connected") return;
      this.sendKey("ShiftLeft");
    }, 300);
  }

  screenshot(): string | null {
    try {
      return this.canvas?.toDataURL("image/png") ?? null;
    } catch {
      return null;
    }
  }

  // ── clipboard and files ──────────────────────────────────────────────────────────────────

  private onGuestClipboard(text: string): void {
    this.events.onGuestClipboard?.(text);
    if (navigator.clipboard?.writeText && document.hasFocus()) {
      navigator.clipboard.writeText(text).catch(() => {
        // the browser refused (no permission, or the page is not focused); the panel still has it
      });
    }
  }

  /** Offer the browser's clipboard to the guest (read on focus, so a paste there finds it). */
  private syncHostClipboard = (): void => {
    const sc = this.sc;
    if (!sc?.agent_connected || !navigator.clipboard?.readText) return;
    navigator.clipboard.readText().then((text) => {
      if (!text || text === this.hostClipboard) return;
      this.hostClipboard = text;
      sc.clipboard_grab?.();
    }).catch(() => {
      // permission denied or not focused; typed paste remains
    });
  };

  /** Hand text to the guest's clipboard (agent present) so its next paste uses it. */
  setGuestClipboard(text: string): boolean {
    if (!this.sc?.agent_connected || !this.sc.clipboard_grab) return false;
    this.hostClipboard = text;
    this.sc.clipboard_grab();
    return true;
  }

  sendFiles(files: FileList | File[]): number {
    const sc = this.sc;
    if (!sc?.agent_connected || !sc.file_xfer_start) return 0;
    let started = 0;
    for (const file of Array.from(files)) {
      if (!file.size && !file.type) continue; // a directory
      sc.file_xfer_start(file);
      started += 1;
    }
    return started;
  }

  cancelFile(id: number): void {
    this.sc?.file_xfer_cancel?.(id);
  }

  // ── statistics ───────────────────────────────────────────────────────────────────────────

  private startStats(): void {
    this.stopStats();
    this.lastDraws = 0;
    this.bytes = 0;
    this.statsTimer = setInterval(() => {
      const sc = this.sc;
      if (!sc) return;
      for (const channel of [sc, sc.display, sc.inputs, sc.cursor] as (ChannelLike | undefined)[]) {
        const ws = channel?.ws;
        if (ws && !this.trackedSockets.has(ws)) {
          this.trackedSockets.add(ws);
          ws.addEventListener("message", (event: MessageEvent) => {
            this.bytes += (event.data as ArrayBuffer).byteLength ?? 0;
          });
        }
      }
      const draws = sc.draws ?? 0;
      const fps = Math.max(0, draws - this.lastDraws);
      this.lastDraws = draws;
      const bytesPerSecond = this.bytes;
      this.bytes = 0;
      this.events.onStats?.({ rttMs: this.rtt === null ? null : Math.round(this.rtt), fps, bytesPerSecond });
    }, 1000);
  }

  private stopStats(): void {
    if (this.statsTimer) clearInterval(this.statsTimer);
    this.statsTimer = null;
  }
}
