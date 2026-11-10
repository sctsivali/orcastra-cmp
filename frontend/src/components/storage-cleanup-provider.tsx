"use client";

/** Cleans up localStorage when the app starts, so it stays under the browser's quota. */

import * as React from "react";
import { 
  checkAndCleanupStorage, 
  getAppStorageSize,
  getStorageBreakdown,
  emergencyCleanup,
  clearSessionStorage,
  nuclearCleanup,
} from "@/lib/storage-utils";

// Startup size above which session storage is cleared and the emergency cleanup runs.
const STARTUP_CLEANUP_THRESHOLD = 1 * 1024 * 1024;

// Startup size above which the nuclear cleanup runs; browsers typically allow 5 to 10 MB.
const NUCLEAR_CLEANUP_THRESHOLD = 3 * 1024 * 1024;

interface StorageCleanupContextType {
  storageSize: number;
  getBreakdown: () => Record<string, number>;
  runCleanup: () => void;
  runEmergencyCleanup: () => void;
  runNuclearCleanup: () => void;
}

const StorageCleanupContext = React.createContext<StorageCleanupContextType | undefined>(undefined);

export function StorageCleanupProvider({ children }: { children: React.ReactNode }) {
  const [storageSize, setStorageSize] = React.useState(0);
  
  React.useEffect(() => {
    if (typeof window === 'undefined') return;
    
    const size = getAppStorageSize();
    setStorageSize(size);
    
    if (size > NUCLEAR_CLEANUP_THRESHOLD) {
      nuclearCleanup();
    }
    else if (size > STARTUP_CLEANUP_THRESHOLD) {
      clearSessionStorage();
      emergencyCleanup();
    } else {
      checkAndCleanupStorage();
    }
    
    const newSize = getAppStorageSize();
    setStorageSize(newSize);
  }, []);
  
  const runCleanup = React.useCallback(() => {
    checkAndCleanupStorage();
    setStorageSize(getAppStorageSize());
  }, []);
  
  const runEmergencyCleanup = React.useCallback(() => {
    emergencyCleanup();
    setStorageSize(getAppStorageSize());
  }, []);
  
  const runNuclearCleanup = React.useCallback(() => {
    nuclearCleanup();
    setStorageSize(getAppStorageSize());
  }, []);
  
  const getBreakdown = React.useCallback(() => {
    return getStorageBreakdown();
  }, []);
  
  const value = React.useMemo(() => ({
    storageSize,
    getBreakdown,
    runCleanup,
    runEmergencyCleanup,
    runNuclearCleanup,
  }), [storageSize, getBreakdown, runCleanup, runEmergencyCleanup, runNuclearCleanup]);
  
  return (
    <StorageCleanupContext.Provider value={value}>
      {children}
    </StorageCleanupContext.Provider>
  );
}

export function useStorageCleanup() {
  const context = React.useContext(StorageCleanupContext);
  if (!context) {
    // A component outside the provider gets an inert fallback.
    return {
      storageSize: 0,
      getBreakdown: () => ({}),
      runCleanup: () => {},
      runEmergencyCleanup: () => {},
      runNuclearCleanup: () => {},
    };
  }
  return context;
}
