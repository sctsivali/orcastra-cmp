"use client";

/**
 * Storage Cleanup Provider
 * 
 * Runs storage cleanup operations on app initialization to prevent
 * localStorage quota issues. This is especially important for
 * enterprise applications with many users.
 */

import * as React from "react";
import { 
  checkAndCleanupStorage, 
  getAppStorageSize,
  getStorageBreakdown,
  emergencyCleanup,
  clearSessionStorage,
  nuclearCleanup,
} from "@/lib/storage-utils";

// threshold for aggressive cleanup on startup (1MB)
const STARTUP_CLEANUP_THRESHOLD = 1 * 1024 * 1024;

// threshold for nuclear cleanup (3MB - browsers typically allow 5-10MB)
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
  
  // run cleanup on mount
  React.useEffect(() => {
    if (typeof window === 'undefined') return;
    
    const size = getAppStorageSize();
    setStorageSize(size);
    
    // If storage is VERY high, run nuclear cleanup
    if (size > NUCLEAR_CLEANUP_THRESHOLD) {
      nuclearCleanup();
    }
    // If storage is over normal threshold, run aggressive cleanup
    else if (size > STARTUP_CLEANUP_THRESHOLD) {
      clearSessionStorage();
      emergencyCleanup();
    } else {
      // run normal proactive cleanup
      checkAndCleanupStorage();
    }
    
    // update size after cleanup
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
    // return a fallback for components that might not be wrapped
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
