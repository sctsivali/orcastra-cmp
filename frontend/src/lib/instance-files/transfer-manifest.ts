import type { Transfer } from "./transfer-store";

/**
 * What survives a page reload: enough to offer "choose the same file to resume".
 *
 * A browser forgets every File handle on reload, so bytes cannot continue by themselves, and no
 * cancel fires on `pagehide`, or the reload would destroy what could have resumed. An abandoned temp
 * expires on its own.
 *
 * note: sessionStorage, so it ends with the tab. It holds a signed upload token, which is useless
 * without the same signed-in person's bearer.
 */
export type ManifestEntry = Transfer & { lastModified: number; actor?: string };

const KEY = "orcastra.instance-files.uploads";

// The person these entries belong to. A tab outlives a sign-in, and the next person must not see
// the last one's hosts, instances and paths.
let currentActor = "";

export function setManifestActor(actor: string): void {
  currentActor = actor;
  write(read());
}

function read(): ManifestEntry[] {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    const entries = raw ? (JSON.parse(raw) as ManifestEntry[]) : [];
    return entries.filter((entry) => entry.actor === currentActor);
  } catch {
    return [];
  }
}

function write(entries: ManifestEntry[]): void {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(entries));
  } catch {
    // Storage blocked or full: a reload simply cannot resume.
  }
}

export function readManifest(): ManifestEntry[] {
  return typeof window === "undefined" ? [] : read();
}

export function rememberUpload(entry: ManifestEntry): void {
  write([...read().filter((item) => item.id !== entry.id), { ...entry, actor: currentActor }]);
}

export function forgetUpload(id: string): void {
  write(read().filter((item) => item.id !== id));
}

export function clearManifest(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // Nothing to clear.
  }
}
