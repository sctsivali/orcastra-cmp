"use client";

import * as React from "react";
import { onAuthActorChanged } from "@/lib/api";
import { captureActorMutationFence, isActorMutationFenceCurrent } from "@/lib/actor-mutation-fence";
import { DEFAULT_PERSONALIZATION, type PersonalizationSettings, type ThemePreference } from "@/types/personalization";
import { readOrgPreferences, readOrgVersion, readPersonalPreferences, sameOrgVersion, type OrgPreferencesVersion } from "./preferences";

type SettingsRef = React.MutableRefObject<PersonalizationSettings | null>;

/** Whose theme is on screen: the account's own choice, or its organization's default. */
export type ThemeOrigin = { personal: boolean; organizationTheme: ThemePreference };

interface SyncOptions {
  userId: string | null;
  scopeKey: string;
  accessToken?: string;
  authenticated: boolean;
  sessionLoading: boolean;
  personalRowRef: SettingsRef;
  lastLocalEditRef: React.MutableRefObject<number>;
  setSettings: React.Dispatch<React.SetStateAction<PersonalizationSettings>>;
  setIsLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setThemeOrigin: React.Dispatch<React.SetStateAction<ThemeOrigin>>;
}

const ORG_PREF_POLL_INTERVAL_MS = 45_000;

// Every account reads the same two endpoints. The server answers my-preferences with the
// deployment default for an Administrator without the partner role, and the personal row holds
// only the theme for everyone.
export function usePersonalizationSync({
  userId, scopeKey, accessToken, authenticated, sessionLoading,
  personalRowRef, lastLocalEditRef, setSettings, setIsLoading, setThemeOrigin,
}: SyncOptions) {
  const appliedVersionRef = React.useRef<OrgPreferencesVersion | null>(null);
  const requestRevisionRef = React.useRef(0);
  const currentScopeRef = React.useRef(scopeKey);
  const previousScopeRef = React.useRef<string | null>(null);
  const checkInFlightRef = React.useRef(false);
  currentScopeRef.current = scopeKey;
  // important: read the bearer through a ref. As a dependency every silent rotation (about every
  // four minutes) would re-run all three preference reads and restart the poll. An actor change
  // is fenced by scopeKey and onAuthActorChanged.
  const accessTokenRef = React.useRef(accessToken);
  accessTokenRef.current = accessToken;

  const refreshSettings = React.useCallback(async (expectedVersion?: OrgPreferencesVersion): Promise<boolean> => {
    if (!userId || !authenticated) return false;
    const actorFence = captureActorMutationFence(scopeKey);
    const revision = ++requestRevisionRef.current;
    const localEdit = lastLocalEditRef.current;
    const isCurrent = () => currentScopeRef.current === scopeKey && requestRevisionRef.current === revision && isActorMutationFenceCurrent(actorFence);
    const canApply = () => isCurrent() && lastLocalEditRef.current === localEdit;

    const [organization, personal] = await Promise.all([
      readOrgPreferences(accessTokenRef.current, isCurrent),
      readPersonalPreferences(accessTokenRef.current, isCurrent),
    ]);
    if (!organization.ok || !personal.ok || !canApply()) return false;
    // important: membership or preferences may change while the payload is in flight. Read the
    // version after the payload and only apply the organization/version pair still current.
    const current = await readOrgVersion(accessTokenRef.current, isCurrent);
    if (!current.ok || !sameOrgVersion(organization.value, current.value) || !canApply()) return false;
    if (expectedVersion && !sameOrgVersion(expectedVersion, current.value)) return false;
    const base = organization.value.settings;
    const personalTheme = Boolean(personal.value.lastModified);
    personalRowRef.current = personal.value;
    setSettings({ ...base, theme: personalTheme ? personal.value.theme : base.theme });
    setThemeOrigin({ personal: personalTheme, organizationTheme: base.theme });
    // The baseline is advanced only after a complete, fenced payload read has been applied.
    // A null updated_at is a valid baseline, so the organization's first save triggers refresh.
    appliedVersionRef.current = current.value;
    return true;
  }, [userId, authenticated, scopeKey, lastLocalEditRef, personalRowRef, setSettings, setThemeOrigin]);

  React.useEffect(() => onAuthActorChanged(() => {
    requestRevisionRef.current += 1;
    appliedVersionRef.current = null;
  }), []);

  React.useEffect(() => {
    if (sessionLoading) return;
    const changed = previousScopeRef.current !== scopeKey;
    if (changed) {
      requestRevisionRef.current += 1;
      appliedVersionRef.current = null;
      personalRowRef.current = null;
      if (previousScopeRef.current !== null) setSettings(DEFAULT_PERSONALIZATION);
      previousScopeRef.current = scopeKey;
    }
    if (!authenticated || !userId) {
      setSettings(DEFAULT_PERSONALIZATION);
      setIsLoading(false);
      return;
    }
    let retired = false;
    setIsLoading(true);
    void refreshSettings().finally(() => {
      if (!retired && currentScopeRef.current === scopeKey) setIsLoading(false);
    });
    return () => { retired = true; requestRevisionRef.current += 1; };
  }, [sessionLoading, authenticated, userId, scopeKey, refreshSettings, personalRowRef, setIsLoading, setSettings]);

  React.useEffect(() => {
    if (!authenticated || !userId) return;
    let retired = false;
    const check = async () => {
      if (retired || checkInFlightRef.current) return;
      checkInFlightRef.current = true;
      const actorFence = captureActorMutationFence(scopeKey);
      const isCurrent = () => !retired && currentScopeRef.current === scopeKey && isActorMutationFenceCurrent(actorFence);
      try {
        const current = await readOrgVersion(accessTokenRef.current, isCurrent);
        if (current.ok && isCurrent() && !sameOrgVersion(appliedVersionRef.current, current.value)) {
          await refreshSettings(current.value);
        }
      } finally {
        checkInFlightRef.current = false;
      }
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") void check();
    };
    const interval = setInterval(() => void check(), ORG_PREF_POLL_INTERVAL_MS);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      retired = true;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [authenticated, userId, scopeKey, refreshSettings]);

  return { refreshSettings: React.useCallback(async () => { await refreshSettings(); }, [refreshSettings]) };
}
