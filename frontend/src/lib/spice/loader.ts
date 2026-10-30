/**
 * Loads the vendored spice-html5 scripts (public/spice-html5) once per page.
 *
 * note: every script tag is inserted at once with `async = false`, so the browser downloads them
 * in parallel and still runs them in document order. They used to be appended one at a time,
 * each waiting for the previous `onload`, a serial waterfall of ~29 round trips on a first open.
 * A failed script now rejects the load (it used to be skipped, surfacing later as a
 * ReferenceError deep inside a connect).
 */

const BASE = "/spice-html5";

// Order matters for execution: main.js needs every channel class before it.
const SCRIPTS = [
  "enums.js",
  "atKeynames.js",
  "utils.js",
  "png.js",
  "lz.js",
  "quic.js",
  "bitmap.js",
  "spicearraybuffer.js",
  "spicedataview.js",
  "spicetype.js",
  "spicemsg.js",
  "wire.js",
  "spiceconn.js",
  "display.js",
  "inputs.js",
  "cursor.js",
  "simulatecursor.js",
  "playback.js",
  "port.js",
  "thirdparty/jsbn.js",
  "thirdparty/rsa.js",
  "thirdparty/prng4.js",
  "thirdparty/rng.js",
  "thirdparty/sha1.js",
  "ticket.js",
  "main.js",
  "filexfer.js",
  "webm.js",
];

let loading: Promise<void> | null = null;

function inject(name: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const src = `${BASE}/${name}`;
    const existing = document.querySelector<HTMLScriptElement>(`script[data-spice="${name}"]`);
    if (existing?.dataset.loaded === "1") {
      resolve();
      return;
    }
    const el = existing ?? document.createElement("script");
    el.addEventListener("load", () => {
      el.dataset.loaded = "1";
      resolve();
    }, { once: true });
    el.addEventListener("error", () => reject(new Error(`The console client could not load ${name}`)), { once: true });
    if (!existing) {
      el.src = src;
      el.async = false;
      el.dataset.spice = name;
      document.head.appendChild(el);
    }
  });
}

/** Resolves once SpiceMainConn exists; a later call after a failure tries again. */
export function loadSpice(): Promise<void> {
  if (typeof window === "undefined") return Promise.reject(new Error("The console runs in the browser"));
  if ((window as unknown as { SpiceMainConn?: unknown }).SpiceMainConn) return Promise.resolve();
  if (!loading) {
    loading = Promise.all(SCRIPTS.map(inject)).then(() => {
      if (!(window as unknown as { SpiceMainConn?: unknown }).SpiceMainConn) {
        throw new Error("The console client did not start");
      }
    });
    loading.catch(() => {
      // Let the next connect retry, with fresh tags for whatever failed.
      document.querySelectorAll<HTMLScriptElement>("script[data-spice]").forEach((el) => {
        if (el.dataset.loaded !== "1") el.remove();
      });
      loading = null;
    });
  }
  return loading;
}
