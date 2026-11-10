/**
 * localStorage access that never throws, size estimates, and cleanup of
 * persisted console and terminal sessions.
 *
 * localStorage holds roughly 5-10 MB depending on the browser. A write that
 * hits the quota falls back to progressively wider cleanup.
 */

// Storage keys this module manages.
export const STORAGE_KEYS = {
  CONSOLE_STORE: 'orcastra-console-store',
  TERMINAL_STORE: 'orcastra-terminals',
  SIDEBAR_COLLAPSED: 'sidebar-collapsed',
  THEME: 'theme',
} as const;

// Persisted console and terminal sessions idle longer than this are dropped.
const MAX_STALE_AGE = 1 * 60 * 60 * 1000;

// Most consoles and terminals persisted at once.
const MAX_PERSISTED_SESSIONS = 3;

// Rough localStorage budget, kept well below the smallest browser quota.
const APP_STORAGE_LIMIT = 2 * 1024 * 1024;

/** Estimated bytes for a value stored as JSON in localStorage. */
export function estimateStorageSize(value: unknown): number {
  try {
    const json = JSON.stringify(value);
    // localStorage stores UTF-16, so each character is 2 bytes.
    return json.length * 2;
  } catch {
    return 0;
  }
}

/** Bytes used by every key in this origin's localStorage. */
export function getAppStorageSize(): number {
  if (typeof window === 'undefined') return 0;
  
  let totalSize = 0;
  
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key) {
      const value = localStorage.getItem(key);
      if (value) {
        totalSize += (key.length + value.length) * 2;
      }
    }
  }
  
  return totalSize;
}

/** Bytes used by the keys this module manages, matched by prefix. */
export function getManagedStorageSize(): number {
  if (typeof window === 'undefined') return 0;
  
  let totalSize = 0;
  const prefixes = ['orcastra-', 'sidebar-', 'theme'];
  
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && prefixes.some(p => key.startsWith(p))) {
      const value = localStorage.getItem(key);
      if (value) {
        totalSize += (key.length + value.length) * 2;
      }
    }
  }
  
  return totalSize;
}

/** Bytes per localStorage key, for debugging. */
export function getStorageBreakdown(): Record<string, number> {
  if (typeof window === 'undefined') return {};
  
  const breakdown: Record<string, number> = {};
  
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key) {
      const value = localStorage.getItem(key);
      if (value) {
        breakdown[key] = (key.length + value.length) * 2;
      }
    }
  }
  
  return breakdown;
}

/** Drop console and terminal sessions inactive for longer than `maxAge`. */
export function cleanupStaleSessions<T extends { lastActiveAt?: number; createdAt?: number }>(
  sessions: T[],
  maxAge: number = MAX_STALE_AGE
): T[] {
  const now = Date.now();
  return sessions.filter(session => {
    const lastActive = session.lastActiveAt || session.createdAt || 0;
    return (now - lastActive) < maxAge;
  });
}

/** Keep only the `maxCount` most recently active sessions. */
export function limitSessionCount<T extends { lastActiveAt?: number; createdAt?: number }>(
  sessions: T[],
  maxCount: number = MAX_PERSISTED_SESSIONS
): T[] {
  if (sessions.length <= maxCount) return sessions;
  
  const sorted = [...sessions].sort((a, b) => {
    const aTime = a.lastActiveAt || a.createdAt || 0;
    const bTime = b.lastActiveAt || b.createdAt || 0;
    return bTime - aTime;
  });
  
  return sorted.slice(0, maxCount);
}

/** setItem that never throws; false when the write failed (ex; quota). */
export function safeSetItem(key: string, value: string): boolean {
  if (typeof window === 'undefined') return false;
  
  try {
    localStorage.setItem(key, value);
    return true;
  } catch (error) {
    if (error instanceof DOMException && 
        (error.name === 'QuotaExceededError' || error.code === 22)) {
      return false;
    }
    return false;
  }
}

export function safeGetItem(key: string): string | null {
  if (typeof window === 'undefined') return null;
  
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function safeRemoveItem(key: string): void {
  if (typeof window === 'undefined') return;
  
  try {
    localStorage.removeItem(key);
  } catch {
    // Silently ignore removal errors
  }
}

/** Remove the persisted console and terminal stores from localStorage. */
export function clearSessionStorage(): void {
  if (typeof window === 'undefined') return;
  
  safeRemoveItem(STORAGE_KEYS.CONSOLE_STORE);
  safeRemoveItem(STORAGE_KEYS.TERMINAL_STORE);
}

/**
 * First fallback when the quota is exceeded: removes every `orcastra-` key,
 * plus any other key over 100,000 characters that looks like regenerable
 * cache or state.
 */
export function emergencyCleanup(): void {
  if (typeof window === 'undefined') return;
  
  const preserveKeys = new Set(['theme', 'sidebar-collapsed']);

  // Collect first: removing while iterating shifts the indexes.
  const keysToRemove: string[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key) {
      if (key.startsWith('orcastra-')) {
        if (!preserveKeys.has(key)) {
          keysToRemove.push(key);
        }
      }
      // Large keys from other sources are a common cause of quota errors.
      else {
        const value = localStorage.getItem(key);
        if (value && value.length > 100000) { // About 200 KB in UTF-16.
          // Names that usually hold cache or state that can be regenerated.
          const largeKeyPatterns = [
            /^persist:/,        // redux-persist
            /^cache/i,          // general cache
            /^draft/i,          // drafts
            /-state$/,          // state snapshots
            /^__/,              // internal keys
          ];
          if (largeKeyPatterns.some(pattern => pattern.test(key))) {
            keysToRemove.push(key);
          }
        }
      }
    }
  }
  
  for (const key of keysToRemove) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Ignored: the remaining keys are still tried.
    }
  }

  // Also drop the persisted console and terminal stores.
  clearSessionStorage();

}

/** Clear the console and terminal stores past 80% of APP_STORAGE_LIMIT. */
export function checkAndCleanupStorage(): void {
  if (typeof window === 'undefined') return;

  const currentSize = getAppStorageSize();
  const warningThreshold = APP_STORAGE_LIMIT * 0.8;
  
  if (currentSize > warningThreshold) {
    clearSessionStorage();
  }
}

/** Last resort after emergencyCleanup: remove every key but the preserved. */
export function nuclearCleanup(): void {
  if (typeof window === 'undefined') return;

  const preserveKeys = new Set(['theme', 'sidebar-collapsed']);

  const keysToRemove: string[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && !preserveKeys.has(key)) {
      keysToRemove.push(key);
    }
  }
  
  for (const key of keysToRemove) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Ignored: the remaining keys are still tried.
    }
  }
}

/**
 * A storage adapter for Zustand's persist middleware. Applies `cleanupFn`
 * before each write and, on a failed write, retries after emergencyCleanup
 * and then nuclearCleanup.
 */
export function createSafeStorage<T>(options?: {
  onQuotaExceeded?: () => void;
  cleanupFn?: (data: T) => T;
}) {
  return {
    getItem: (name: string): string | null => {
      return safeGetItem(name);
    },
    setItem: (name: string, value: string): void => {
      if (options?.cleanupFn) {
        try {
          const parsed = JSON.parse(value);
          const cleaned = options.cleanupFn(parsed);
          value = JSON.stringify(cleaned);
        } catch {
          // An unparseable value is stored as given.
        }
      }
      
      const success = safeSetItem(name, value);
      
      if (!success) {
        emergencyCleanup();

        let retrySuccess = safeSetItem(name, value);

        if (!retrySuccess) {
          nuclearCleanup();
          retrySuccess = safeSetItem(name, value);
        }
        
        if (!retrySuccess) {
          options?.onQuotaExceeded?.();
        }
      }
    },
    removeItem: (name: string): void => {
      safeRemoveItem(name);
    },
  };
}
