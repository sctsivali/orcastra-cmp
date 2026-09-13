/**
 * The rules a profile form can check without a round trip, and the shapes it edits.
 *
 * LXD validates config keys and device bodies itself with specific messages, so only what an
 * operator can fix on the spot lives here: the name rule (the backend applies the same one),
 * empty or duplicate keys, empty or duplicate device names, a device with no type.
 */

// The house rule the backend applies on create: a letter first, then letters, digits, dots,
// underscores and hyphens, at most 63. Stricter than LXD on purpose.
export const PROFILE_NAME_RE = /^[a-zA-Z][a-zA-Z0-9._-]*$/;
export const PROFILE_NAME_MAX = 63;
// LXD has no cap. The dialog shows a count past this so a long description is a choice, not
// a surprise; a description written elsewhere that is longer still edits and saves.
export const DESCRIPTION_MAX = 255;

export const STALE_PROFILE_MESSAGE =
  "This profile changed since you opened it. Reload and apply your change again.";

// The device types LXD knows. "none" masks a device inherited from another profile.
export const DEVICE_TYPES = [
  "disk",
  "nic",
  "gpu",
  "usb",
  "unix-char",
  "unix-block",
  "unix-hotplug",
  "proxy",
  "infiniband",
  "tpm",
  "pci",
  "none",
] as const;

export interface ConfigRow {
  id: string;
  key: string;
  value: string;
}

export interface DeviceDraft {
  id: string;
  name: string;
  type: string;
  props: ConfigRow[];
}

let seq = 0;
export function rowId(): string {
  seq += 1;
  return `r${seq}`;
}

export function profileNameError(name: string): string {
  if (name === "") return "";
  if (!PROFILE_NAME_RE.test(name)) {
    return "Must start with a letter and contain only letters, numbers, dots, underscores and hyphens.";
  }
  if (name.length > PROFILE_NAME_MAX) return `Name must be at most ${PROFILE_NAME_MAX} characters.`;
  return "";
}

/** Never an error: LXD accepts any length. Kept as the one place the rule is stated. */
export function descriptionError(_description: string): string {
  return "";
}

/** Per-row problems, keyed by row id; empty when every row is fine. */
export function configRowErrors(rows: ConfigRow[], reserved: readonly string[] = []): Record<string, string> {
  const out: Record<string, string> = {};
  const seen = new Set<string>();
  for (const row of rows) {
    const key = row.key.trim();
    // a row just added is blank by definition; it blocks the save (the footer says why) but is
    // not painted as a mistake until something has been typed into it
    if (!key) out[row.id] = row.value.trim() ? "Enter a key" : "";
    else if (/\s/.test(key)) out[row.id] = "A key cannot contain spaces";
    else if (reserved.includes(key)) out[row.id] = "Set the type with the selector above";
    else if (seen.has(key)) out[row.id] = "Duplicate key";
    seen.add(key);
  }
  return out;
}

export interface DeviceErrors {
  name?: string;
  type?: string;
  props: Record<string, string>;
}

export function deviceErrors(drafts: DeviceDraft[]): Record<string, DeviceErrors> {
  const out: Record<string, DeviceErrors> = {};
  const seen = new Set<string>();
  for (const draft of drafts) {
    const errors: DeviceErrors = { props: configRowErrors(draft.props, ["type"]) };
    const name = draft.name.trim();
    if (!name) errors.name = draft.type || draft.props.length ? "Enter a device name" : "";
    else if (/[\s/]/.test(name)) errors.name = "A device name cannot contain spaces or slashes";
    else if (seen.has(name)) errors.name = "Duplicate device name";
    seen.add(name);
    if (!draft.type) errors.type = name ? "Select a type" : "";
    if (errors.name || errors.type || Object.values(errors.props).some(Boolean)) out[draft.id] = errors;
  }
  return out;
}

export function rowsToMap(rows: ConfigRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (key) out[key] = row.value;
  }
  return out;
}

export function mapToRows(map: Record<string, string> | undefined): ConfigRow[] {
  return Object.entries(map ?? {}).map(([key, value]) => ({ id: rowId(), key, value: String(value ?? "") }));
}

export function devicesToDrafts(devices: Record<string, Record<string, string>> | undefined): DeviceDraft[] {
  return Object.entries(devices ?? {}).map(([name, body]) => {
    const { type, ...props } = body ?? {};
    return { id: rowId(), name, type: String(type ?? ""), props: mapToRows(props) };
  });
}

export function draftsToDevices(drafts: DeviceDraft[]): Record<string, Record<string, string>> {
  const out: Record<string, Record<string, string>> = {};
  for (const draft of drafts) {
    const name = draft.name.trim();
    if (!name) continue;
    out[name] = { type: draft.type, ...rowsToMap(draft.props) };
  }
  return out;
}
