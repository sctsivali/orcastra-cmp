"use client";

import * as React from "react";
import { onAuthActorChanged } from "@/lib/api";
import { captureActorMutationFence, isActorMutationFenceCurrent } from "@/lib/actor-mutation-fence";
import { DEFAULT_PERSONALIZATION, type PersonalizationSettings } from "@/types/personalization";
import { readOrgPreferences, readOrgVersion, readPersonalPreferences, sameOrgVersion, type OrgPreferencesVersion } from "./preferences";

type SettingsRef = React.MutableRefObject<PersonalizationSettings | null>;

interface SyncOptions {
  userId: string | null;
  scopeKey: string;
  accessToken?: string;
  authenticated: boolean;
  sessionLoading: boolean;
  orgBacked: boolean;
  personalRowRef: SettingsRef;
  lastLocalEditRef: React.MutableRefObject<number>;
  setSettings: React.Dispatch<React.SetStateAction<PersonalizationSettings>>;
  setIsLoading: React.Dispatch<React.SetStateAction<boolean>>;
}

const ORG_PREF_POLL_INTERVAL_MS = 45_000;

export function usePersonalizationSync({
  userId, scopeKey, accessToken, authenticated, sessionLoading, orgBacked,
  personalRowRef, lastLocalEditRef, setSettings, setIsLoading,
}: SyncOptions) {
  const appliedVersionRef = React.useRef<OrgPreferencesVersion | null>(null);
  const requestRevisionRef = React.useRef(0);
  const currentScopeRef = React.useRef(scopeKey);
  const previousScopeRef = React.useRef<string | null>(null);
  const checkInFlightRef = React.useRef(false);
  currentScopeRef.current = scopeKey;

  const refreshSettings = React.useCallback(async (expectedVersion?: OrgPreferencesVersion): Promise<boolean> => {
    if (!userId || !authenticated) return false;
    const actorFence = captureActorMutationFence(scopeKey);
    const revision = ++requestRevisionRef.current;
    const localEdit = lastLocalEditRef.current;
    const isCurrent = () => currentScopeRef.current === scopeKey && requestRevisionRef.current === revision && isActorMutationFenceCurrent(actorFence);
    const canApply = () => isCurrent() && lastLocalEditRef.current === localEdit;

    if (!orgBacked) {
      const personal = await readPersonalPreferences(accessToken, isCurrent);
      if (!personal.ok || !canApply()) return false;
      personalRowRef.current = personal.value;
      setSettings(personal.value);
      return true;
    }

    const [organization, personal] = await Promise.all([
      readOrgPreferences(accessToken, isCurrent),
      readPersonalPreferences(accessToken, isCurrent),
    ]);
    if (!organization.ok || !personal.ok || !canApply()) return false;
    // important: membership or preferences may change while the payload is in flight. Read the
    // version after the payload and only apply the organization/version pair still current.
    const current = await readOrgVersion(accessToken, isCurrent);
    if (!current.ok || !sameOrgVersion(organization.value, current.value) || !canApply()) return false;
    if (expectedVersion && !sameOrgVersion(expectedVersion, current.value)) return false;
    const base = organization.value.settings;
    personalRowRef.current = personal.value;
    setSettings({ ...base, theme: personal.value.lastModified ? personal.value.theme : base.theme });
    // The baseline is advanced only after a complete, fenced payload read has been applied.
    // A null updated_at is a valid baseline, so the organization's first save triggers refresh.
    appliedVersionRef.current = current.value;
    return true;
  }, [userId, authenticated, scopeKey, accessToken, orgBacked, lastLocalEditRef, personalRowRef, setSettings]);

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
  }, [sessionLoading, authenticated, userId, scopeKey, orgBacked, refreshSettings, personalRowRef, setIsLoading, setSettings]);

  React.useEffect(() => {
    if (!authenticated || !userId || !orgBacked) return;
    let retired = false;
    const check = async () => {
      if (retired || checkInFlightRef.current) return;
      checkInFlightRef.current = true;
      const actorFence = captureActorMutationFence(scopeKey);
      const isCurrent = () => !retired && currentScopeRef.current === scopeKey && isActorMutationFenceCurrent(actorFence);
      try {
        const current = await readOrgVersion(accessToken, isCurrent);
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
  }, [authenticated, userId, scopeKey, accessToken, orgBacked, refreshSettings]);

  return { refreshSettings: React.useCallback(async () => { await refreshSettings(); }, [refreshSettings]) };
}
