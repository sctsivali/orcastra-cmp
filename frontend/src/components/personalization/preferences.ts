import { DEFAULT_PERSONALIZATION, isIllustrationPreference, type PersonalizationSettings } from "@/types/personalization";
import { safeImageSrc } from "@/lib/branding-images";
import { toByteUnitSystem, type ByteUnitSystem } from "@/lib/byte-units";

// `version` is the server's token over every layer under the account (its organization, that
// organization's provider, the deployment default) and the link between them. A timestamp alone
// misses a reset or a provider change that leaves an older row on top.
export type OrgPreferencesVersion = { organization_id: number; updated_at: string | null; version: string | null };
export type OrgPreferences = OrgPreferencesVersion & { settings: PersonalizationSettings };
export type PreferenceRead<T> = { ok: true; value: T } | { ok: false };
/** The account's own row: the defaults with its theme over them, and its byte units. */
export type PersonalPreferences = { settings: PersonalizationSettings; byteUnits: ByteUnitSystem };

export function mergeWithDefaults(data: Partial<PersonalizationSettings>): PersonalizationSettings {
  const logo = { ...DEFAULT_PERSONALIZATION.logo, ...data.logo };
  const background = { ...DEFAULT_PERSONALIZATION.background, ...data.background };
  return {
    ...DEFAULT_PERSONALIZATION,
    ...data,
    // Image sources are checked again here: a stored row may predate server-side validation,
    // and these end up in a CSS url(), a favicon link and an <img>.
    logo: {
      lightModeLogo: safeImageSrc(logo.lightModeLogo),
      darkModeLogo: safeImageSrc(logo.darkModeLogo),
      favicon: safeImageSrc(logo.favicon),
    },
    background: { ...background, backgroundImage: safeImageSrc(background.backgroundImage) },
    branding: { ...DEFAULT_PERSONALIZATION.branding, ...data.branding },
    colors: { ...DEFAULT_PERSONALIZATION.colors, ...data.colors },
    theme: data.theme ?? DEFAULT_PERSONALIZATION.theme,
    illustrations: isIllustrationPreference(data.illustrations) ? data.illustrations : DEFAULT_PERSONALIZATION.illustrations,
  };
}

export function sameOrgVersion(a: OrgPreferencesVersion | null, b: OrgPreferencesVersion): boolean {
  return a !== null && a.organization_id === b.organization_id && a.updated_at === b.updated_at && a.version === b.version;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function parseOrgVersion(value: unknown): OrgPreferencesVersion | null {
  if (!isRecord(value) || !Number.isSafeInteger(value.organization_id) || (value.organization_id as number) < 0) return null;
  if (value.updated_at !== null && typeof value.updated_at !== "string") return null;
  const version = typeof value.version === "string" ? value.version : null;
  return { organization_id: value.organization_id as number, updated_at: value.updated_at, version };
}

async function readPreferences(
  path: string,
  accessToken: string | undefined,
  isCurrent: () => boolean,
): Promise<PreferenceRead<unknown>> {
  for (let attempt = 0; attempt <= 2; attempt += 1) {
    if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, 500 * attempt));
    if (!isCurrent()) return { ok: false };
    try {
      const response = await fetch(`/api/v1/${path}`, {
        credentials: "include",
        cache: "no-store",
        headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
        signal: AbortSignal.timeout(10_000),
      });
      if (!isCurrent()) return { ok: false };
      if (response.ok) {
        const value: unknown = await response.json();
        return isCurrent() ? { ok: true, value } : { ok: false };
      }
      if (response.status >= 400 && response.status < 500) return { ok: false };
    } catch {
      // Read failures may be retried; the mutation helpers never replay a write.
    }
  }
  return { ok: false };
}

export async function readPersonalPreferences(
  accessToken: string | undefined,
  isCurrent: () => boolean,
): Promise<PreferenceRead<PersonalPreferences>> {
  const result = await readPreferences("user-preferences", accessToken, isCurrent);
  if (!result.ok || !isRecord(result.value)) return { ok: false };
  return {
    ok: true,
    value: {
      settings: mergeWithDefaults(result.value as Partial<PersonalizationSettings>),
      byteUnits: toByteUnitSystem(result.value.byteUnits),
    },
  };
}

// A personal choice saved in one tab reaches the account's other tabs at once instead of at
// their next reload. Same origin only; the message names the account scope, never a value.
const PERSONAL_WRITE_CHANNEL = "orcastra-personal-preferences";

export function announcePersonalWrite(scopeKey: string): void {
  if (typeof BroadcastChannel === "undefined") return;
  try {
    const channel = new BroadcastChannel(PERSONAL_WRITE_CHANNEL);
    channel.postMessage({ scopeKey });
    channel.close();
  } catch {
    // Another tab then picks the choice up on its next load.
  }
}

/** Calls `onWrite` when another tab of the same account saved a personal choice. */
export function onPersonalWrite(scopeKey: string, onWrite: () => void): () => void {
  if (typeof BroadcastChannel === "undefined") return () => {};
  let channel: BroadcastChannel;
  try {
    channel = new BroadcastChannel(PERSONAL_WRITE_CHANNEL);
  } catch {
    return () => {};
  }
  channel.onmessage = (event: MessageEvent) => {
    const data: unknown = event.data;
    if (isRecord(data) && data.scopeKey === scopeKey) onWrite();
  };
  return () => channel.close();
}

export async function readOrgPreferences(
  accessToken: string | undefined,
  isCurrent: () => boolean,
): Promise<PreferenceRead<OrgPreferences>> {
  const result = await readPreferences("organizations/my-preferences", accessToken, isCurrent);
  if (!result.ok || !isRecord(result.value) || !isRecord(result.value.settings)) return { ok: false };
  const version = parseOrgVersion(result.value);
  return version
    ? { ok: true, value: { ...version, settings: mergeWithDefaults(result.value.settings) } }
    : { ok: false };
}

export async function readOrgVersion(
  accessToken: string | undefined,
  isCurrent: () => boolean,
): Promise<PreferenceRead<OrgPreferencesVersion>> {
  const result = await readPreferences("organizations/my-preferences/version", accessToken, isCurrent);
  const version = result.ok ? parseOrgVersion(result.value) : null;
  return version ? { ok: true, value: version } : { ok: false };
}
