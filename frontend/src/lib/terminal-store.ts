/**
 * Zustand store for terminal windows: state, positions and persistence.
 *
 * Persists to localStorage through createSafeStorage (quota-safe), keeps the five most recent
 * sessions and drops stale ones on load.
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import {
  createSafeStorage,
  cleanupStaleSessions,
  limitSessionCount,
  STORAGE_KEYS,
} from './storage-utils';
import { useBreakGlassStore } from './break-glass-store';

/** Viewport pixels; terminal windows use position: fixed. */
export interface TerminalPosition {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type TerminalStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

export type SnapLayout = 'none' | 'left' | 'right' | 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right' | 'full';

/** The instance's own LXD state, separate from the terminal connection status. */
export type LXDInstanceStatus = 'Running' | 'Stopped' | 'Frozen' | 'Error' | 'Unknown' | 'Starting' | 'Stopping' | 'Restarting';

/** One terminal window. */
export interface TerminalInstance {
  id: string;
  instanceName: string;
  host: string;
  project: string;
  title: string;
  status: TerminalStatus;
  instanceStatus: LXDInstanceStatus;
  minimized: boolean;
  maximized: boolean;
  focused: boolean;
  sticky: boolean; // Stays fixed on screen while the page scrolls.
  position: TerminalPosition;
  preMaximizePosition?: TerminalPosition;
  snapLayout: SnapLayout;
  createdAt: number;
  lastActiveAt: number;
  errorMessage?: string;
  sessionId?: string;
}

interface TerminalState {
  terminals: TerminalInstance[];
  focusedTerminalId: string | null;
  // Terminal ids, back to front.
  zIndexOrder: string[];
  maxTerminals: number;
  defaultSize: { width: number; height: number };
  trayExpanded: boolean;

  openTerminal: (instance: {
    instanceName: string;
    host: string;
    project?: string;
  }) => Promise<TerminalInstance | null>;
  closeTerminal: (id: string) => void;
  closeAllTerminals: () => void;
  minimizeTerminal: (id: string) => void;
  restoreTerminal: (id: string) => void;
  restoreAllTerminals: () => void;
  focusTerminal: (id: string) => void;
  updateTerminalPosition: (id: string, position: Partial<TerminalPosition>) => void;
  updateTerminalStatus: (id: string, status: TerminalStatus, errorMessage?: string) => void;
  setTerminalSessionId: (id: string, sessionId: string) => void;
  setSnapLayout: (id: string, layout: SnapLayout) => void;
  toggleTray: () => void;
  bringToFront: (id: string) => void;
  getTerminal: (id: string) => TerminalInstance | undefined;
  getTerminalByInstance: (instanceName: string, host: string) => TerminalInstance | undefined;
  maximizeTerminal: (id: string) => void;
  restoreFromMaximize: (id: string) => void;
  reorderMinimizedTerminals: (fromIndex: number, toIndex: number) => void;
  arrangeAllTerminals: () => void;
  toggleStickyTerminal: (id: string) => void;
  updateInstanceStatus: (id: string, instanceStatus: LXDInstanceStatus) => void;
  ensureWithinViewport: () => void;
}

// The task dock reserves the bottom of the viewport. TaskPanel writes its measured height to
// --dock-height on the root element, so the usable height here is the viewport minus that.
function readDockHeight(): number {
  if (typeof document === 'undefined') return 0;
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--dock-height');
  const parsed = parseFloat(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function generateTerminalId(): string {
  return `term_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

// Terminals use position: fixed, so coordinates are relative to the viewport.
function getViewportCenteredPosition(index: number, defaultSize: { width: number; height: number }): TerminalPosition {
  const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
  const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 800;
  
  // Cascade offset for multiple windows
  const offset = (index % 5) * 30;
  
  const x = (viewportWidth - defaultSize.width) / 2 + offset;
  const y = (viewportHeight - defaultSize.height) / 2 + offset;
  
  return {
    x: Math.max(10, x),
    y: Math.max(60, y), // Below the 60px header.
    width: defaultSize.width,
    height: defaultSize.height,
  };
}

export const useTerminalStore = create<TerminalState>()(
  persist(
    (set, get) => ({
      terminals: [],
      focusedTerminalId: null,
      zIndexOrder: [],
      maxTerminals: 8,
      defaultSize: { width: 800, height: 500 },
      trayExpanded: true,

      openTerminal: async ({ instanceName, host, project = 'default' }) => {
        const state = get();

        // One terminal per instance and host: restore and focus an open one.
        const existing = state.terminals.find(
          t => t.instanceName === instanceName && t.host === host
        );
        
        if (existing) {
          set(s => ({
            terminals: s.terminals.map(t =>
              t.id === existing.id
                ? { ...t, minimized: false, focused: true, lastActiveAt: Date.now() }
                : { ...t, focused: false }
            ),
            focusedTerminalId: existing.id,
            zIndexOrder: [...s.zIndexOrder.filter(id => id !== existing.id), existing.id],
          }));
          return existing;
        }
        
        if (state.terminals.length >= state.maxTerminals) {
          if (process.env.NODE_ENV !== 'production') {
            console.warn(`Maximum terminals (${state.maxTerminals}) reached`);
          }
          return null;
        }

        // Break-glass pre-flight: an admin opening a tenant cluster they do not own must
        // justify the access first. Owned/in-scope access resolves immediately.
        const bgAllowed = await useBreakGlassStore.getState().request(host, instanceName);
        if (!bgAllowed) return null;

        const id = generateTerminalId();
        const position = getViewportCenteredPosition(state.terminals.length, state.defaultSize);
        
        const newTerminal: TerminalInstance = {
          id,
          instanceName,
          host,
          project,
          title: `${instanceName}@${host}`,
          status: 'connecting',
          instanceStatus: 'Running', // Assumed: a terminal is only opened on a running instance.
          minimized: false,
          maximized: false,
          focused: true,
          sticky: true,
          position,
          snapLayout: 'none',
          createdAt: Date.now(),
          lastActiveAt: Date.now(),
        };
        
        set(s => ({
          terminals: [...s.terminals.map(t => ({ ...t, focused: false })), newTerminal],
          focusedTerminalId: id,
          zIndexOrder: [...s.zIndexOrder, id],
        }));
        
        return newTerminal;
      },

      closeTerminal: (id) => {
        set(s => {
          const newTerminals = s.terminals.filter(t => t.id !== id);
          const newZIndexOrder = s.zIndexOrder.filter(zId => zId !== id);
          const newFocusedId = s.focusedTerminalId === id
            ? newZIndexOrder[newZIndexOrder.length - 1] || null
            : s.focusedTerminalId;
          
          return {
            terminals: newTerminals,
            zIndexOrder: newZIndexOrder,
            focusedTerminalId: newFocusedId,
          };
        });
      },

      closeAllTerminals: () => {
        set({
          terminals: [],
          zIndexOrder: [],
          focusedTerminalId: null,
        });
      },

      minimizeTerminal: (id) => {
        set(s => {
          const newFocusedId = s.focusedTerminalId === id
            ? s.zIndexOrder.filter(zId => zId !== id).pop() || null
            : s.focusedTerminalId;
          
          return {
            terminals: s.terminals.map(t =>
              t.id === id ? { ...t, minimized: true, focused: false } : t
            ),
            focusedTerminalId: newFocusedId,
          };
        });
      },

      restoreTerminal: (id) => {
        set(s => {
          const terminal = s.terminals.find(t => t.id === id);
          if (!terminal) {
            if (process.env.NODE_ENV !== 'production') {
              console.warn('[TerminalStore] restoreTerminal: terminal not found', id);
            }
            return s;
          }
          
          const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
          const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 800;
          
          // Restoring re-centers the window in the viewport.
          const newPosition = {
            ...terminal.position,
            x: Math.max(20, (viewportWidth - terminal.position.width) / 2),
            y: Math.max(60, (viewportHeight - terminal.position.height) / 2),
          };
          
          return {
            terminals: s.terminals.map(t =>
              t.id === id
                ? { ...t, minimized: false, focused: true, lastActiveAt: Date.now(), position: newPosition }
                : { ...t, focused: false }
            ),
            focusedTerminalId: id,
            zIndexOrder: [...s.zIndexOrder.filter(zId => zId !== id), id],
          };
        });
      },

      restoreAllTerminals: () => {
        set(s => {
          const minimized = s.terminals.filter(t => t.minimized);
          if (minimized.length === 0) return s;
          
          const lastRestoredId = minimized[minimized.length - 1].id;
          return {
            terminals: s.terminals.map(t =>
              t.minimized
                ? { ...t, minimized: false, focused: t.id === lastRestoredId, lastActiveAt: Date.now() }
                : { ...t, focused: false }
            ),
            focusedTerminalId: lastRestoredId,
            zIndexOrder: [...s.zIndexOrder],
          };
        });
      },

      focusTerminal: (id) => {
        set(s => ({
          terminals: s.terminals.map(t =>
            t.id === id
              ? { ...t, focused: true, lastActiveAt: Date.now() }
              : { ...t, focused: false }
          ),
          focusedTerminalId: id,
          zIndexOrder: [...s.zIndexOrder.filter(zId => zId !== id), id],
        }));
      },

      updateTerminalPosition: (id, position) => {
        set(s => ({
          terminals: s.terminals.map(t =>
            t.id === id
              ? { ...t, position: { ...t.position, ...position } }
              : t
          ),
        }));
      },

      updateTerminalStatus: (id, status, errorMessage) => {
        set(s => ({
          terminals: s.terminals.map(t =>
            t.id === id
              ? { ...t, status, errorMessage }
              : t
          ),
        }));
      },

      setTerminalSessionId: (id, sessionId) => {
        set(s => ({
          terminals: s.terminals.map(t =>
            t.id === id ? { ...t, sessionId } : t
          ),
        }));
      },

      setSnapLayout: (id, layout) => {
        set(s => ({
          terminals: s.terminals.map(t =>
            t.id === id ? { ...t, snapLayout: layout } : t
          ),
        }));
      },

      toggleTray: () => {
        set(s => ({ trayExpanded: !s.trayExpanded }));
      },

      bringToFront: (id) => {
        set(s => ({
          zIndexOrder: [...s.zIndexOrder.filter(zId => zId !== id), id],
          focusedTerminalId: id,
          terminals: s.terminals.map(t =>
            t.id === id
              ? { ...t, focused: true, lastActiveAt: Date.now() }
              : { ...t, focused: false }
          ),
        }));
      },

      getTerminal: (id) => {
        return get().terminals.find(t => t.id === id);
      },

      getTerminalByInstance: (instanceName, host) => {
        return get().terminals.find(
          t => t.instanceName === instanceName && t.host === host
        );
      },

      maximizeTerminal: (id) => {
        set(s => ({
          terminals: s.terminals.map(t => {
            if (t.id === id && !t.maximized) {
              const viewport = {
                width: typeof window !== 'undefined' ? window.innerWidth : 1200,
                height: typeof window !== 'undefined' ? window.innerHeight : 800,
              };
              const fullPosition = getSnapPosition('full', viewport.width, viewport.height);
              return {
                ...t,
                maximized: true,
                preMaximizePosition: { ...t.position },
                position: fullPosition,
              };
            }
            return t;
          }),
        }));
      },

      restoreFromMaximize: (id) => {
        set(s => ({
          terminals: s.terminals.map(t => {
            if (t.id === id && t.maximized && t.preMaximizePosition) {
              return {
                ...t,
                maximized: false,
                position: { ...t.preMaximizePosition },
                preMaximizePosition: undefined,
              };
            }
            return t;
          }),
        }));
      },

      reorderMinimizedTerminals: (fromIndex, toIndex) => {
        set(s => {
          const minimized = s.terminals.filter(t => t.minimized);
          const notMinimized = s.terminals.filter(t => !t.minimized);
          
          if (fromIndex < 0 || fromIndex >= minimized.length || 
              toIndex < 0 || toIndex >= minimized.length) {
            return s;
          }
          
          // Minimized terminals move to the end of the list, in their new tray order.
          const [removed] = minimized.splice(fromIndex, 1);
          minimized.splice(toIndex, 0, removed);
          
          return {
            terminals: [...notMinimized, ...minimized],
          };
        });
      },

      arrangeAllTerminals: () => {
        set(s => {
          const activeTerminals = s.terminals.filter(t => !t.minimized);
          if (activeTerminals.length === 0) return s;

          const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
          const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 800;
          const padding = 10;
          const headerHeight = 60;
          
          const count = activeTerminals.length;
          let cols: number, rows: number;
          
          if (count === 1) {
            cols = 1; rows = 1;
          } else if (count === 2) {
            cols = 2; rows = 1;
          } else if (count <= 4) {
            cols = 2; rows = 2;
          } else if (count <= 6) {
            cols = 3; rows = 2;
          } else {
            cols = 3; rows = Math.ceil(count / 3);
          }
          
          const cellWidth = (viewportWidth - padding * (cols + 1)) / cols;
          const cellHeight = (viewportHeight - headerHeight - padding * (rows + 1)) / rows;
          
          const positionMap = new Map<string, TerminalPosition>();
          activeTerminals.forEach((terminal, index) => {
            const col = index % cols;
            const row = Math.floor(index / cols);
            
            positionMap.set(terminal.id, {
              x: padding + col * (cellWidth + padding),
              y: headerHeight + padding + row * (cellHeight + padding),
              width: cellWidth,
              height: cellHeight,
            });
          });
          
          return {
            terminals: s.terminals.map(t => {
              if (t.minimized) return t;
              const newPos = positionMap.get(t.id);
              if (!newPos) return t;
              return {
                ...t,
                position: newPos,
                maximized: false,
                preMaximizePosition: undefined,
                snapLayout: 'none' as SnapLayout,
              };
            }),
          };
        });
      },

      toggleStickyTerminal: (id) => {
        set(s => ({
          terminals: s.terminals.map(t => {
            if (t.id !== id) return t;
            // Position stays; the component handles scroll tracking.
            return {
              ...t,
              sticky: !t.sticky,
            };
          }),
        }));
      },

      updateInstanceStatus: (id, instanceStatus) => {
        set(s => ({
          terminals: s.terminals.map(t =>
            t.id === id ? { ...t, instanceStatus } : t
          ),
        }));
      },
      ensureWithinViewport: () => {
        // Called on viewport resize and rotation.
        const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
        const viewportHeight = (typeof window !== 'undefined' ? window.innerHeight : 800) - readDockHeight();
        const padding = 20;
        const minVisible = 100;

        set(s => ({
          terminals: s.terminals.map(t => {
            if (t.minimized) return t;

            let { x, y, width, height } = t.position;
            
            const isMobileViewport = viewportWidth < 768;
            if (isMobileViewport) {
              width = Math.min(width, viewportWidth - padding * 2);
              height = Math.min(height, viewportHeight - 80); // Leave room for the header.
            }

            // At least minVisible px of the window stays inside the viewport.
            const maxX = viewportWidth - minVisible;
            const maxY = viewportHeight - minVisible;
            
            x = Math.max(padding, Math.min(x, maxX));
            y = Math.max(60, Math.min(y, maxY)); // Below the 60px header.

            if (x + width > viewportWidth - padding) {
              width = Math.max(300, viewportWidth - x - padding);
            }
            if (y + height > viewportHeight - padding) {
              height = Math.max(200, viewportHeight - y - padding);
            }

            if (x === t.position.x && y === t.position.y && 
                width === t.position.width && height === t.position.height) {
              return t;
            }

            return {
              ...t,
              position: { x, y, width, height },
            };
          }),
        }));
      },    }),
    {
      name: STORAGE_KEYS.TERMINAL_STORE,
      storage: createJSONStorage(() => createSafeStorage<{
        state: { terminals: TerminalInstance[]; zIndexOrder: string[]; trayExpanded: boolean };
      }>({
        onQuotaExceeded: () => {
          console.warn('[TerminalStore] Storage quota exceeded, some sessions may not be persisted');
        },
        cleanupFn: (data) => {
          if (data?.state?.terminals) {
            // Drops sessions idle longer than MAX_STALE_AGE in storage-utils (one hour).
            let terminals = cleanupStaleSessions(data.state.terminals);
            terminals = limitSessionCount(terminals, 5);
            return {
              ...data,
              state: {
                ...data.state,
                terminals,
                zIndexOrder: data.state.zIndexOrder.filter((id: string) => 
                  terminals.some(t => t.id === id)
                ),
              },
            };
          }
          return data;
        },
      })),
      partialize: (state) => ({
        // Only these fields persist, capped at the five most recent sessions to bound storage use.
        terminals: limitSessionCount(
          cleanupStaleSessions(state.terminals),
          5
        ).map(t => ({
          ...t,
          // Connection state does not survive a reload.
          status: 'disconnected' as TerminalStatus,
          instanceStatus: 'Unknown' as LXDInstanceStatus, // Refreshed on reconnect.
          focused: false,
          sessionId: undefined,
          errorMessage: undefined,
        })),
        zIndexOrder: state.zIndexOrder,
        trayExpanded: state.trayExpanded,
      }),
      onRehydrateStorage: () => (state) => {
        if (state) {
          const cleaned = cleanupStaleSessions(state.terminals);
          const limited = limitSessionCount(cleaned, 5);
          if (limited.length !== state.terminals.length) {
            state.terminals = limited;
            state.zIndexOrder = state.zIndexOrder.filter(id => 
              limited.some(t => t.id === id)
            );
          }
        }
      },
    }
  )
);

export function useTerminalZIndex(terminalId: string): number {
  const zIndexOrder = useTerminalStore(s => s.zIndexOrder);
  const baseZIndex = 1000;
  const index = zIndexOrder.indexOf(terminalId);
  return index === -1 ? baseZIndex : baseZIndex + index;
}

export function getSnapPosition(
  layout: SnapLayout,
  viewportWidth: number,
  viewportHeight: number
): TerminalPosition {
  const padding = 10;
  const halfWidth = (viewportWidth - padding * 3) / 2;
  const halfHeight = (viewportHeight - padding * 3) / 2;
  const fullWidth = viewportWidth - padding * 2;
  const fullHeight = viewportHeight - padding * 2 - 60; // Minus the 60px header.

  switch (layout) {
    case 'left':
      return { x: padding, y: padding + 60, width: halfWidth, height: fullHeight };
    case 'right':
      return { x: halfWidth + padding * 2, y: padding + 60, width: halfWidth, height: fullHeight };
    case 'top-left':
      return { x: padding, y: padding + 60, width: halfWidth, height: halfHeight };
    case 'top-right':
      return { x: halfWidth + padding * 2, y: padding + 60, width: halfWidth, height: halfHeight };
    case 'bottom-left':
      return { x: padding, y: halfHeight + padding * 2 + 60, width: halfWidth, height: halfHeight };
    case 'bottom-right':
      return { x: halfWidth + padding * 2, y: halfHeight + padding * 2 + 60, width: halfWidth, height: halfHeight };
    case 'full':
      return { x: padding, y: padding + 60, width: fullWidth, height: fullHeight };
    default:
      return { x: 100, y: 100, width: 800, height: 500 };
  }
}
