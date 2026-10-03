"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useActorQueryScope } from "@/hooks/use-query-scope";

interface NoticeContext {
  showRestrictedNotice: () => void;
  dismissNotice: () => void;
}

const SettingsNoticeContext = createContext<NoticeContext | null>(null);

export function SettingsNoticeProvider({ children }: { children: React.ReactNode }) {
  const { scopeKey } = useActorQueryScope();
  const actorRef = useRef(scopeKey);
  const [restricted, setRestricted] = useState(false);
  const showRestrictedNotice = useCallback(() => setRestricted(true), []);
  const dismissNotice = useCallback(() => setRestricted(false), []);

  useEffect(() => {
    if (actorRef.current !== scopeKey) {
      actorRef.current = scopeKey;
      setRestricted(false);
    }
  }, [scopeKey]);

  return (
    <SettingsNoticeContext.Provider value={{ showRestrictedNotice, dismissNotice }}>
      <div className="space-y-6">
        {restricted && (
          <Alert>
            <ShieldAlert className="h-4 w-4" />
            <AlertTitle>Settings access restricted</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>This settings tab requires Partner access. General settings are available to you.</p>
              <Button variant="outline" size="sm" onClick={dismissNotice} aria-label="Dismiss settings notice">Dismiss</Button>
            </AlertDescription>
          </Alert>
        )}
        {children}
      </div>
    </SettingsNoticeContext.Provider>
  );
}

export function useSettingsNotice(): NoticeContext {
  const context = useContext(SettingsNoticeContext);
  if (!context) throw new Error("Settings notices require SettingsNoticeProvider");
  return context;
}
