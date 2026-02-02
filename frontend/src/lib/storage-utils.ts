/**
 * Storage Utilities for Enterprise-Grade localStorage Management
 * 
 * Handles:
 * - Quota management with LRU eviction
 * - Safe localStorage operations with error handling
 * - Automatic cleanup of stale data
 * - Size estimation and limits
 * 
 * IMPORTANT: localStorage has a ~5-10MB limit (browser dependent).
 * This module ensures we stay within limits and gracefully handle quota errors.
 */

// storage keys we manage
export const STORAGE_KEYS = {
  CONSOLE_STORE: 'orcastra-console-store',
  TERMINAL_STORE: 'orcastra-terminals',
  SIDEBAR_COLLAPSED: 'sidebar-collapsed',
  THEME: 'theme',
} as const;

// maximum age for stale data (1 hour in ms - reduced from 24h)
const MAX_STALE_AGE = 1 * 60 * 60 * 1000;

// maximum number of consoles/terminals to persist (reduced from 5)
const MAX_PERSISTED_SESSIONS = 3;

// rough size limit for localStorage usage by our app (2MB to be safer)
const APP_STORAGE_LIMIT = 2 * 1024 * 1024;

/**
 * Estimate the size of a value in bytes when stored as JSON in localStorage
 */
export function estimateStorageSize(value: unknown): number {
  try {
    const json = JSON.stringify(value);
    // localStorage uses UTF-16, so each character is 2 bytes
    return json.length * 2;
  } catch {
    return 0;
  }
}

/**
 * Get the current size of all our app's localStorage usage
 */
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

/**
 * Get the size of our managed keys only
 */
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

/**
 * Get detailed storage breakdown for debugging
 */
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

/**
 * Clean up stale console/terminal sessions from store data
 */
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

/**
 * Limit the number of persisted sessions (keep most recent)
 */
export function limitSessionCount<T extends { lastActiveAt?: number; createdAt?: number }>(
  sessions: T[],
  maxCount: number = MAX_PERSISTED_SESSIONS
): T[] {
  if (sessions.length <= maxCount) return sessions;
  
  // sort by lastActiveAt (newest first)
  const sorted = [...sessions].sort((a, b) => {
    const aTime = a.lastActiveAt || a.createdAt || 0;
    const bTime = b.lastActiveAt || b.createdAt || 0;
    return bTime - aTime;
  });
  
  return sorted.slice(0, maxCount);
}

/**
 * Safe localStorage setItem with quota handling
 * Returns true if successful, false if quota exceeded
 */
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

/**
 * Safe localStorage getItem
 */
export function safeGetItem(key: string): string | null {
  if (typeof window === 'undefined') return null;
  
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * Safe localStorage removeItem
 */
export function safeRemoveItem(key: string): void {
  if (typeof window === 'undefined') return;
  
  try {
    localStorage.removeItem(key);
  } catch {
    // Silently ignore removal errors
  }
}

/**
 * Clear all console and terminal data from localStorage
 * Useful when storage is full
 */
export function clearSessionStorage(): void {
  if (typeof window === 'undefined') return;
  
  safeRemoveItem(STORAGE_KEYS.CONSOLE_STORE);
  safeRemoveItem(STORAGE_KEYS.TERMINAL_STORE);
}

/**
 * Emergency cleanup when quota is exceeded
 * More aggressive - clears all orcastra-* keys except current theme
 * Also clears other potentially large non-essential keys
 */
export function emergencyCleanup(): void {
  if (typeof window === 'undefined') return;
  
  // Keys to always preserve (essential for user experience)
  const preserveKeys = new Set(['theme', 'sidebar-collapsed']);
  
  // Get all keys to remove (can't modify while iterating)
  const keysToRemove: string[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key) {
      // Remove orcastra- keys except critical ones
      if (key.startsWith('orcastra-')) {
        if (!preserveKeys.has(key)) {
          keysToRemove.push(key);
        }
      }
      // Also check for large non-essential keys from other sources
      // that commonly cause quota issues
      else {
        const value = localStorage.getItem(key);
        if (value && value.length > 100000) { // > 100KB per key
          // Large keys that are likely cache/state that can be regenerated
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
  
  // Remove collected keys
  for (const key of keysToRemove) {
    try {
      localStorage.removeItem(key);
    } catch {
      // ignore
    }
  }
  
  // Also try clearing session storage
  clearSessionStorage();

}

/**
 * Check if we're approaching storage limit and proactively cleanup
 */
export function checkAndCleanupStorage(): void {
  if (typeof window === 'undefined') return;
  
  const currentSize = getAppStorageSize();
  const warningThreshold = APP_STORAGE_LIMIT * 0.8; // 80%
  
  if (currentSize > warningThreshold) {
    clearSessionStorage();
  }
}

/**
 * Nuclear option - clear ALL localStorage except essential keys
 * Use when emergencyCleanup doesn't free enough space
 */
export function nuclearCleanup(): void {
  if (typeof window === 'undefined') return;
  
  // Keys to absolutely preserve
  const preserveKeys = new Set(['theme', 'sidebar-collapsed']);
  
  // Get all keys
  const keysToRemove: string[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && !preserveKeys.has(key)) {
      keysToRemove.push(key);
    }
  }
  
  // Remove all
  for (const key of keysToRemove) {
    try {
      localStorage.removeItem(key);
    } catch {
      // ignore
    }
  }
}

/**
 * Create a safe storage adapter for Zustand persist middleware
 * Handles quota errors gracefully and applies data cleanup
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
      // try to apply cleanup if provided
      if (options?.cleanupFn) {
        try {
          const parsed = JSON.parse(value);
          const cleaned = options.cleanupFn(parsed);
          value = JSON.stringify(cleaned);
        } catch {
          // ignore parsing errors
        }
      }
      
      const success = safeSetItem(name, value);
      
      if (!success) {
        // quota exceeded - try emergency cleanup first
        emergencyCleanup();
        
        // retry
        let retrySuccess = safeSetItem(name, value);
        
        if (!retrySuccess) {
          // still failing - try nuclear cleanup as last resort
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
