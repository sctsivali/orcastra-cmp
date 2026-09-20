/**
 * Console Store - Zustand state management for console windows.
 * Handles console state, positions, capabilities, and persistence.
 * 
 * Supports:
 * - Text console (xterm.js) - for containers and VMs
 * - Graphics console (SPICE via spice-html5) - for VMs only
 * 
 * LXD/Incus uses SPICE protocol for VM graphics console.
 * 
 * Storage Notes:
 * - Uses localStorage with safe error handling and quota management
 * - Only persists the most recent N console sessions (configurable)
 * - Cleans up stale sessions on load
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import {
  createSafeStorage,
  cleanupStaleSessions,
  limitSessionCount,
  STORAGE_KEYS,
} from './storage-utils';
import { getAccessToken } from './api';
import { useBreakGlassStore } from './break-glass-store';

// Console window position and size
export interface ConsolePosition {
  x: number;
  y: number;
  width: number;
  height: number;
}

// Console connection state
export type ConsoleConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error';

// Console types
export type ConsoleType = 'text' | 'graphics';

// Graphics protocol type
// LXD/Incus uses SPICE for VM graphics console
export type GraphicsType = 'spice' | 'vnc' | 'none';

// Active tab in console window
export type ConsoleTab = 'text' | 'graphics' | 'boot' | 'iso';

// LXD instance status
export type InstanceStatus = 'Running' | 'Stopped' | 'Frozen' | 'Error' | 'Unknown' | 'Starting' | 'Stopping' | 'Restarting';

// Console capabilities (detected from backend)
export interface ConsoleCapabilities {
  textConsole: boolean;
  graphicsConsole: boolean;  // True for VMs, uses SPICE protocol
  graphicsType: GraphicsType;  // 'spice' for LXD/Incus VMs
  graphicsAddress?: string;
  graphicsPort?: number;
  audioEnabled: boolean;  // SPICE audio passthrough
  usbRedirect: boolean;   // SPICE USB redirect
  isVM: boolean;          // Helper flag
  state?: string;         // Instance state
}

// Startup order entry
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

// ISO info
export interface ISOInfo {
  name: string;
  pool: string;
  volume: string;
  path: string;
  size: number;
  createdAt?: string;
}

// Attached ISO
export interface AttachedISO {
  deviceName: string;
  source: string;
  pool?: string;
  bootPriority: number;
}

// Single console instance
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
  fullPage: boolean; // Full page mode
  focused: boolean;
  position: ConsolePosition;
  preMaximizePosition?: ConsolePosition;
  createdAt: number;
  lastActiveAt: number;
  errorMessage?: string;
  sessionId?: string;
  operationId?: string;  // LXD operation ID for cleanup
  // Graphics specific
  graphicsWidth: number;
  graphicsHeight: number;
  // ISO info
  attachedISO?: AttachedISO;
}

// Store state
interface ConsoleState {
  // Console instances
  consoles: ConsoleInstance[];
  // Currently focused console
  focusedConsoleId: string | null;
  // Z-index ordering
  zIndexOrder: string[];
  // Maximum consoles allowed
  maxConsoles: number;
  // Default console size
  defaultSize: { width: number; height: number };
  // Default graphics size
  defaultGraphicsSize: { width: number; height: number };
  
  // Actions
  openConsole: (params: {
    instanceName: string;
    instanceType: string;
    host: string;
    project?: string;
    fullPage?: boolean;
  }) => Promise<ConsoleInstance | null>;
  closeConsole: (id: string, cancelOperation?: boolean) => void;
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

// Generate unique console ID
function generateConsoleId(): string {
  return `console_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

// Calculate viewport-centered position for new windows
function getViewportCenteredPosition(
  index: number, 
  defaultSize: { width: number; height: number }
): ConsolePosition {
  const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
  const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 800;
  
  // Start from center, cascade for multiple windows
  const cascadeOffset = index * 30;
  const centerX = (viewportWidth - defaultSize.width) / 2 + cascadeOffset;
  const centerY = (viewportHeight - defaultSize.height) / 2 + cascadeOffset;
  
  // Ensure window stays within viewport bounds
  const x = Math.max(50, Math.min(centerX, viewportWidth - defaultSize.width - 50));
  const y = Math.max(50, Math.min(centerY, viewportHeight - defaultSize.height - 50));
  
  return {
    x,
    y,
    width: defaultSize.width,
    height: defaultSize.height,
  };
}

// Create console store
export const useConsoleStore = create<ConsoleState>()(
  persist(
    (set, get) => ({
      // Initial state
      consoles: [],
      focusedConsoleId: null,
      zIndexOrder: [],
      maxConsoles: 8,
      defaultSize: { width: 900, height: 650 },
      defaultGraphicsSize: { width: 1024, height: 768 },
      
      // Open new console
      openConsole: async (params) => {
        const { instanceName, instanceType, host, project = 'default', fullPage = false } = params;
        const state = get();
        
        // Check if console for this instance already exists
        const existing = state.consoles.find(
          c => c.instanceName === instanceName && c.host === host && c.project === project
        );
        
        if (existing) {
          // Focus and restore existing console
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
        
        // Check max limit
        if (state.consoles.length >= state.maxConsoles) {
          if (process.env.NODE_ENV !== 'production') {
            console.warn(`Maximum console limit (${state.maxConsoles}) reached`);
          }
          return null;
        }

        // Break-glass pre-flight: an admin opening a tenant cluster they do not own must
        // justify the access first. Owned/in-scope access resolves immediately.
        const bgAllowed = await useBreakGlassStore.getState().request(host, instanceName);
        if (!bgAllowed) return null;

        // Create new console instance
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
      
      // Close console and optionally cancel LXD operation
      closeConsole: (id, cancelOperation = true) => {
        const state = get();
        const consoleToClose = state.consoles.find(c => c.id === id);
        
        // Kill all sessions for this instance if requested
        if (cancelOperation && consoleToClose?.instanceName && consoleToClose?.host) {
          const query = new URLSearchParams({
            host: consoleToClose.host,
            project: consoleToClose.project || "default",
          });
          fetch(`/api/v1/console/${encodeURIComponent(consoleToClose.instanceName)}/kill-all-sessions?${query.toString()}`, {
            method: 'POST',
            headers: {
              ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
            },
          }).then((res) => res.json()).then(() => {
            // Silent
          }).catch(() => {
            // Silent fail - sessions might already be closed
          });
        }
        
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
      
      // Close all consoles
      closeAllConsoles: () => {
        set({
          consoles: [],
          focusedConsoleId: null,
          zIndexOrder: [],
        });
      },
      
      // Minimize console
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
      
      // Restore console from minimized
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
      
      // Focus console
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
      
      // Update position
      updateConsolePosition: (id, position) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id 
              ? { ...c, position: { ...c.position, ...position } }
              : c
          ),
        }));
      },
      
      // Update connection status
      updateConnectionStatus: (id, status, errorMessage) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id 
              ? { ...c, connectionStatus: status, errorMessage }
              : c
          ),
        }));
      },
      
      // Set session ID
      setConsoleSessionId: (id, sessionId) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, sessionId } : c
          ),
        }));
      },
      
      // Set operation ID for cleanup when console closes
      setConsoleOperationId: (id, operationId) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, operationId } : c
          ),
        }));
      },
      
      // Set active tab
      setActiveTab: (id, tab) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, activeTab: tab } : c
          ),
        }));
      },
      
      // Set capabilities
      setCapabilities: (id, caps) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, capabilities: caps } : c
          ),
        }));
      },
      
      // Maximize console
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
      
      // Restore from maximize
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
      
      // Toggle full page mode
      toggleFullPage: (id) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, fullPage: !c.fullPage } : c
          ),
        }));
      },
      
      // Bring to front
      bringToFront: (id) => {
        const state = get();
        if (state.zIndexOrder[state.zIndexOrder.length - 1] === id) return;
        
        set((s) => ({
          zIndexOrder: [...s.zIndexOrder.filter(zid => zid !== id), id],
        }));
      },
      
      // Get console by ID
      getConsole: (id) => {
        return get().consoles.find(c => c.id === id);
      },
      
      // Get console by instance
      getConsoleByInstance: (instanceName, host) => {
        return get().consoles.find(
          c => c.instanceName === instanceName && c.host === host
        );
      },
      
      // Update instance status
      updateInstanceStatus: (id, instanceStatus) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, instanceStatus } : c
          ),
        }));
      },
      
      // Set attached ISO
      setAttachedISO: (id, iso) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, attachedISO: iso } : c
          ),
        }));
      },
      
      // Set graphics size
      setGraphicsSize: (id, width, height) => {
        set((s) => ({
          consoles: s.consoles.map(c => 
            c.id === id ? { ...c, graphicsWidth: width, graphicsHeight: height } : c
          ),
        }));
      },
      // Ensure all consoles are within viewport after resize/rotation
      ensureWithinViewport: () => {
        const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1200;
        const viewportHeight = (typeof window !== 'undefined' ? window.innerHeight : 800) - readDockHeight();
        const padding = 20;
        const minVisible = 100;

        set((s) => ({
          consoles: s.consoles.map(c => {
            if (c.minimized || c.fullPage) return c;

            let { x, y, width, height } = c.position;
            
            // On mobile (small screens), resize window to fit viewport
            const isMobileViewport = viewportWidth < 768;
            if (isMobileViewport) {
              width = Math.min(width, viewportWidth - padding * 2);
              height = Math.min(height, viewportHeight - 80);
            }

            // Ensure window is not positioned beyond viewport
            const maxX = viewportWidth - minVisible;
            const maxY = viewportHeight - minVisible;
            
            x = Math.max(padding, Math.min(x, maxX));
            y = Math.max(60, Math.min(y, maxY));

            // If window extends beyond viewport, adjust size
            if (x + width > viewportWidth - padding) {
              width = Math.max(300, viewportWidth - x - padding);
            }
            if (y + height > viewportHeight - padding) {
              height = Math.max(200, viewportHeight - y - padding);
            }

            // Check if position changed
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
          // clean up and limit console sessions before persisting
          if (data?.state?.consoles) {
            // remove stale sessions (older than 24h)
            let consoles = cleanupStaleSessions(data.state.consoles);
            // keep only most recent 5 sessions
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
        // Only persist basic info, not connection state
        // Also limit to 5 most recent sessions to prevent storage bloat
        consoles: limitSessionCount(
          cleanupStaleSessions(state.consoles),
          5
        ).map(c => ({
          ...c,
          connectionStatus: 'disconnected' as const,
          sessionId: undefined,
          operationId: undefined,
          errorMessage: undefined,
          capabilities: null, // don't persist capabilities (will be refreshed)
        })),
        zIndexOrder: state.zIndexOrder,
      }),
      // clean up on rehydration
      onRehydrateStorage: () => (state) => {
        if (state) {
          // clean up stale consoles on load
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

// Hook for console operations
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
