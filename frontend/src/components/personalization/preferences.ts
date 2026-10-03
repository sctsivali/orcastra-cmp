import { DEFAULT_PERSONALIZATION, type PersonalizationSettings } from "@/types/personalization";

export type OrgPreferencesVersion = { organization_id: number; updated_at: string | null };
export type OrgPreferences = OrgPreferencesVersion & { settings: PersonalizationSettings };
export type PreferenceRead<T> = { ok: true; value: T } | { ok: false };

export function mergeWithDefaults(data: Partial<PersonalizationSettings>): PersonalizationSettings {
  return {
    ...DEFAULT_PERSONALIZATION,
    ...data,
    logo: { ...DEFAULT_PERSONALIZATION.logo, ...data.logo },
    background: { ...DEFAULT_PERSONALIZATION.background, ...data.background },
    branding: { ...DEFAULT_PERSONALIZATION.branding, ...data.branding },
    colors: { ...DEFAULT_PERSONALIZATION.colors, ...data.colors },
    theme: data.theme ?? DEFAULT_PERSONALIZATION.theme,
  };
}

export function sameOrgVersion(a: OrgPreferencesVersion | null, b: OrgPreferencesVersion): boolean {
  return a !== null && a.organization_id === b.organization_id && a.updated_at === b.updated_at;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function parseOrgVersion(value: unknown): OrgPreferencesVersion | null {
  if (!isRecord(value) || !Number.isSafeInteger(value.organization_id) || (value.organization_id as number) < 0) return null;
  if (value.updated_at !== null && typeof value.updated_at !== "string") return null;
  return { organization_id: value.organization_id as number, updated_at: value.updated_at };
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
      // Read failures may be retried. Mutation helpers deliberately do not replay writes.
    }
  }
  return { ok: false };
}

export async function readPersonalPreferences(
  accessToken: string | undefined,
  isCurrent: () => boolean,
): Promise<PreferenceRead<PersonalizationSettings>> {
  const result = await readPreferences("user-preferences", accessToken, isCurrent);
  return result.ok && isRecord(result.value)
    ? { ok: true, value: mergeWithDefaults(result.value as Partial<PersonalizationSettings>) }
    : { ok: false };
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
