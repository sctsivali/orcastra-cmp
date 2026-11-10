/**
 * Console windows: their state, positions, capabilities and persistence.
 *
 * A window holds the text console (xterm.js, containers and VMs) and, for a VM, the graphics
 * console (SPICE through spice-html5). Windows persist to localStorage through a quota-aware
 * wrapper; only the most recent sessions are kept and stale ones are dropped on load.
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

export interface ConsolePosition {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type ConsoleConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error';

export type ConsoleType = 'text' | 'graphics';

// LXD/Incus VMs use SPICE.
export type GraphicsType = 'spice' | 'vnc' | 'none';

export type ConsoleTab = 'text' | 'graphics' | 'serial' | 'boot' | 'iso' | 'files' | 'credentials';

export type InstanceStatus = 'Running' | 'Stopped' | 'Frozen' | 'Error' | 'Unknown' | 'Starting' | 'Stopping' | 'Restarting';

/** What the backend reports the instance's console supports. */
export interface ConsoleCapabilities {
  textConsole: boolean;
  graphicsConsole: boolean;  // VMs only
  graphicsType: GraphicsType;
  graphicsAddress?: string;
  graphicsPort?: number;
  audioEnabled: boolean;  // SPICE audio passthrough
  usbRedirect: boolean;   // SPICE USB redirect
  isVM: boolean;
  state?: string;
}

export interface StartupOrderEntry {
  instanceName: string;
  host: string;
  project: string;
  priority: number;
  delay: number;
  autostart: boolean;
  instanceType: string;
  status: string;
}

export interface ISOInfo {
  name: string;
  pool: string;
  volume: string;
  path: string;
  size: number;
  createdAt?: string;
}

export interface AttachedISO {
  deviceName: string;
  source: string;
  pool?: string;
  bootPriority: number;
}

/** One console window. */
export interface ConsoleInstance {
  id: string;
  instanceName: string;
  instanceType: string; // container or virtual-machine
  host: string;
  project: string;
  title: string;
  connectionStatus: ConsoleConnectionStatus;
  instanceStatus: InstanceStatus;
  activeTab: ConsoleTab;
  capabilities: ConsoleCapabilities | null;
  minimized: boolean;
  maximized: boolean;
  fullPage: boolean;
  focused: boolean;
  position: ConsolePosition;
  preMaximizePosition?: ConsolePosition;
  createdAt: number;
  lastActiveAt: number;
  errorMessage?: string;
  sessionId?: string;
  operationId?: string;  // LXD operation ID of the console session
  graphicsWidth: number;
  graphicsHeight: number;
  attachedISO?: AttachedISO;
}

interface ConsoleState {
  consoles: ConsoleInstance[];
  focusedConsoleId: string | null;
  // Window ids, back to front.
  zIndexOrder: string[];
  maxConsoles: number;
  defaultSize: { width: number; height: number };
  defaultGraphicsSize: { width: number; height: number };

  openConsole: (params: {
    instanceName: string;
    instanceType: string;
    host: string;
    project?: string;
    fullPage?: boolean;
  }) => Promise<ConsoleInstance | null>;
  closeConsole: (id: string) => void;
  closeAllConsoles: () => void;
  minimizeConsole: (id: string) => void;
  restoreConsole: (id: string) => void;
  focusConsole: (id: string) => void;
  updateConsolePosition: (id: string, position: Partial<ConsolePosition>) => void;
  updateConnectionStatus: (id: string, status: ConsoleConnectionStatus, errorMessage?: string) => void;
  setConsoleSessionId: (id: string, sessionId: string) => void;
  setConsoleOperationId: (id: string, operationId: string) => void;
  setActiveTab: (id: string, tab: ConsoleTab) => void;
  setCapabilities: (id: string, caps: ConsoleCapabilities) => void;
  maximizeConsole: (id: string) => void;
  restoreFromMaximize: (id: string) => void;
  toggleFullPage: (id: string) => void;
  bringToFront: (id: string) => void;
  getConsole: (id: string) => ConsoleInstance | undefined;
  getConsoleByInstance: (instanceName: string, host: string) => ConsoleInstance | undefined;
  updateInstanceStatus: (id: string, instanceStatus: InstanceStatus) => void;
  setAttachedISO: (id: string, iso: AttachedISO | undefined) => void;
  setGraphicsSize: (id: string, width: number, height: number) => void;
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

function generateConsoleId(): string {
  return `console_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

// Centred in the viewport, each further window offset by 30px, kept 50px inside the edges.
function getViewportCenteredPosition(
  index: number, 
  defaultSize: { width: number; height: number }
): ConsolePosition {
  const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
  const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 800;

  const cascadeOffset = index * 30;
  const centerX = (viewportWidth - defaultSize.width) / 2 + cascadeOffset;
  const centerY = (viewportHeight - defaultSize.height) / 2 + cascadeOffset;

  const x = Math.max(50, Math.min(centerX, viewportWidth - defaultSize.width - 50));
  const y = Math.max(50, Math.min(centerY, viewportHeight - defaultSize.height - 50));
  
  return {
    x,
    y,
    width: defaultSize.width,
    height: defaultSize.height,
  };
}

export const useConsoleStore = create<ConsoleState>()(
  persist(
    (set, get) => ({
      consoles: [],
      focusedConsoleId: null,
      zIndexOrder: [],
      maxConsoles: 8,
      defaultSize: { width: 900, height: 650 },
      defaultGraphicsSize: { width: 1024, height: 768 },

      // An instance already open in a window is focused and restored, never opened twice.
      openConsole: async (params) => {
        const { instanceName, instanceType, host, project = 'default', fullPage = false } = params;
        const state = get();

        const existing = state.consoles.find(
          c => c.instanceName === instanceName && c.host === host && c.project === project
        );
        
        if (existing) {
          set((s) => ({
            consoles: s.consoles.map(c => 
              c.id === existing.id 
                ? { ...c, minimized: false, focused: true, lastActiveAt: Date.now(), fullPage }
                : { ...c, focused: false }
            ),
            focusedConsoleId: existing.id,
            zIndexOrder: [...s.zIndexOrder.filter(id => id !== existing.id), existing.id],
          }));
          return existing;
        }

        if (state.consoles.length >= state.maxConsoles) {
          if (process.env.NODE_ENV !== 'production') {
            console.warn(`Maximum console limit (${state.maxConsoles}) reached`);
          }
          return null;
        }

        // Break-glass pre-flight: an admin opening a tenant cluster they do not own must
        // justify the access first. Owned or in-scope access resolves immediately.
        const bgAllowed = await useBreakGlassStore.getState().request(host, instanceName);
        if (!bgAllowed) return null;

        const id = generateConsoleId();
        const position = getViewportCenteredPosition(state.consoles.length, state.defaultSize);
        
        const newConsole: ConsoleInstance = {
          id,
          instanceName,
          instanceType,
          host,
          project,
          title: `${instanceName}@${host}`,
          connectionStatus: 'disconnected',
          instanceStatus: 'Unknown',
          activeTab: instanceType === 'virtual-machine' ? 'graphics' : 'text',
          capabilities: null,
          minimized: false,
          maximized: false,
          fullPage,
          focused: true,
          position,
          createdAt: Date.now(),
          lastActiveAt: Date.now(),
          graphicsWidth: state.defaultGraphicsSize.width,
          graphicsHeight: state.defaultGraphicsSize.height,
        };
        
        set((s) => ({
          consoles: [...s.consoles.map(c => ({ ...c, focused: false })), newConsole],
          focusedConsoleId: id,
          zIndexOrder: [...s.zIndexOrder, id],
        }));
        
        return newConsole;
      },
      
      // important: closing only removes the window. Its sockets close as it unmounts,
      // which ends its own LXD operations. Never cancel every websocket operation on the
      // instance: that would end other people's shells and consoles too.
      closeConsole: (id) => {
        set((s) => {
          const newConsoles = s.consoles.filter(c => c.id !== id);
          const newZOrder = s.zIndexOrder.filter(zid => zid !== id);
          const newFocused = s.focusedConsoleId === id
            ? newZOrder[newZOrder.length - 1] || null
            : s.focusedConsoleId;
          
          return {
            consoles: newConsoles.map(c => ({
              ...c,
              focused: c.id === newFocused,
            })),
            focusedConsoleId: newFocused,
            zIndexOrder: newZOrder,
          };
        });
      },
      
      closeAllConsoles: () => {
        set({
          consoles: [],
          focusedConsoleId: null,
          zIndexOrder: [],
        });
      },
      
      minimizeConsole: (id) => {
        set((s) => {
          const newZOrder = s.zIndexOrder.filter(zid => zid !== id);
          const newFocused = s.focusedConsoleId === id
            ? newZOrder[newZOrder.length - 1] || null
            : s.focusedConsoleId;
          
          return {
            consoles: s.consoles.map(c => 
              c.id === id 
                ? { ...c, minimized: true, focused: false }
                : { ...c, focused: c.id === newFocused }
            ),
            focusedConsoleId: newFocused,
          };
        });
      },
      
      restoreConsole: (id) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id 
              ? { ...c, minimized: false, focused: true, lastActiveAt: Date.now() }
              : { ...c, focused: false }
          ),
          focusedConsoleId: id,
          zIndexOrder: [...s.zIndexOrder.filter(zid => zid !== id), id],
        }));
      },
      
      focusConsole: (id) => {
        set((s) => ({
          consoles: s.consoles.map(c => ({
            ...c,
            focused: c.id === id,
            lastActiveAt: c.id === id ? Date.now() : c.lastActiveAt,
          })),
          focusedConsoleId: id,
          zIndexOrder: [...s.zIndexOrder.filter(zid => zid !== id), id],
        }));
      },
      
      updateConsolePosition: (id, position) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id 
              ? { ...c, position: { ...c.position, ...position } }
              : c
          ),
        }));
      },
      
      updateConnectionStatus: (id, status, errorMessage) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id 
              ? { ...c, connectionStatus: status, errorMessage }
              : c
          ),
        }));
      },
      
      setConsoleSessionId: (id, sessionId) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, sessionId } : c
          ),
        }));
      },
      
      setConsoleOperationId: (id, operationId) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, operationId } : c
          ),
        }));
      },
      
      setActiveTab: (id, tab) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, activeTab: tab } : c
          ),
        }));
      },
      
      setCapabilities: (id, caps) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, capabilities: caps } : c
          ),
        }));
      },
      
      maximizeConsole: (id) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id 
              ? { 
                  ...c, 
                  maximized: true,
                  preMaximizePosition: c.position,
                  position: {
                    x: 0,
                    y: 0,
                    width: typeof window !== 'undefined' ? window.innerWidth : 1200,
                    height: typeof window !== 'undefined' ? window.innerHeight - 60 : 700,
                  },
                }
              : c
          ),
        }));
      },
      
      restoreFromMaximize: (id) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id && c.preMaximizePosition
              ? { ...c, maximized: false, position: c.preMaximizePosition }
              : c.id === id
              ? { ...c, maximized: false }
              : c
          ),
        }));
      },
      
      toggleFullPage: (id) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, fullPage: !c.fullPage } : c
          ),
        }));
      },
      
      bringToFront: (id) => {
        const state = get();
        if (state.zIndexOrder[state.zIndexOrder.length - 1] === id) return;
        
        set((s) => ({
          zIndexOrder: [...s.zIndexOrder.filter(zid => zid !== id), id],
        }));
      },
      
      getConsole: (id) => {
        return get().consoles.find(c => c.id === id);
      },
      
      getConsoleByInstance: (instanceName, host) => {
        return get().consoles.find(
          c => c.instanceName === instanceName && c.host === host
        );
      },
      
      updateInstanceStatus: (id, instanceStatus) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, instanceStatus } : c
          ),
        }));
      },
      
      setAttachedISO: (id, iso) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, attachedISO: iso } : c
          ),
        }));
      },
      
      setGraphicsSize: (id, width, height) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, graphicsWidth: width, graphicsHeight: height } : c
          ),
        }));
      },
      // Pulls every window back into the viewport after a resize or rotation.
      ensureWithinViewport: () => {
        const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
        const viewportHeight = (typeof window !== 'undefined' ? window.innerHeight : 800) - readDockHeight();
        const padding = 20;
        const minVisible = 100;

        set((s) => ({
          consoles: s.consoles.map(c => {
            if (c.minimized || c.fullPage) return c;

            let { x, y, width, height } = c.position;
            
            // Below 768px wide, shrink the window to fit.
            const isMobileViewport = viewportWidth < 768;
            if (isMobileViewport) {
              width = Math.min(width, viewportWidth - padding * 2);
              height = Math.min(height, viewportHeight - 80);
            }

            // Keep at least `minVisible` pixels of the window on screen.
            const maxX = viewportWidth - minVisible;
            const maxY = viewportHeight - minVisible;
            
            x = Math.max(padding, Math.min(x, maxX));
            y = Math.max(60, Math.min(y, maxY));

            if (x + width > viewportWidth - padding) {
              width = Math.max(300, viewportWidth - x - padding);
            }
            if (y + height > viewportHeight - padding) {
              height = Math.max(200, viewportHeight - y - padding);
            }

            // Keep the same object for a window that did not move.
            if (x === c.position.x && y === c.position.y && 
                width === c.position.width && height === c.position.height) {
              return c;
            }

            return {
              ...c,
              position: { x, y, width, height },
            };
          }),
        }));
      },    }),
    {
      name: STORAGE_KEYS.CONSOLE_STORE,
      storage: createJSONStorage(() => createSafeStorage<{
        state: { consoles: ConsoleInstance[]; zIndexOrder: string[] };
      }>({
        onQuotaExceeded: () => {
          console.warn('[ConsoleStore] Storage quota exceeded, some sessions may not be persisted');
        },
        cleanupFn: (data) => {
          // Drop stale sessions (storage-utils MAX_STALE_AGE) and keep the 5 most recent.
          if (data?.state?.consoles) {
            let consoles = cleanupStaleSessions(data.state.consoles);
            consoles = limitSessionCount(consoles, 5);
            return {
              ...data,
              state: {
                ...data.state,
                consoles,
                zIndexOrder: data.state.zIndexOrder.filter((id: string) => 
                  consoles.some(c => c.id === id)
                ),
              },
            };
          }
          return data;
        },
      })),
      partialize: (state) => ({
        // Window layout only, never connection state, for the 5 most recent sessions.
        consoles: limitSessionCount(
          cleanupStaleSessions(state.consoles),
          5
        ).map(c => ({
          ...c,
          connectionStatus: 'disconnected' as const,
          sessionId: undefined,
          operationId: undefined,
          errorMessage: undefined,
          capabilities: null, // re-read from the backend on reopen
        })),
        zIndexOrder: state.zIndexOrder,
      }),
      onRehydrateStorage: () => (state) => {
        if (state) {
          const cleaned = cleanupStaleSessions(state.consoles);
          const limited = limitSessionCount(cleaned, 5);
          if (limited.length !== state.consoles.length) {
            state.consoles = limited;
            state.zIndexOrder = state.zIndexOrder.filter(id => 
              limited.some(c => c.id === id)
            );
          }
        }
      },
    }
  )
);

export function useConsole() {
  const store = useConsoleStore();
  
  return {
    consoles: store.consoles,
    focusedConsoleId: store.focusedConsoleId,
    openConsole: store.openConsole,
    closeConsole: store.closeConsole,
    minimizeConsole: store.minimizeConsole,
    restoreConsole: store.restoreConsole,
    focusConsole: store.focusConsole,
    getConsole: store.getConsole,
    getConsoleByInstance: store.getConsoleByInstance,
  };
}
