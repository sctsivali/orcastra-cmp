/**
 * Graphics Console - SPICE client wrapper for VGA console access.
 * Provides graphics console access to VMs via SPICE protocol.
 * 
 * LXD/Incus uses QEMU with SPICE for VM graphics console.
 * This component uses spice-html5 to render the SPICE stream in browser.
 */

"use client";

import * as React from "react";
import { useEffect, useRef, useCallback, forwardRef, useImperativeHandle, useState } from "react";
import { useConsoleStore } from "@/lib/console-store";
import { Loader2, AlertCircle, Monitor, RefreshCw, Wifi, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useManualRefresh } from "@/hooks/use-manual-refresh";

interface GraphicsConsoleProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  width?: number;
  height?: number;
  zoom?: number; // Zoom percentage (100 = 100%)
  onReady?: () => void;
  onError?: (error: string) => void;
  onClose?: () => void;
  className?: string;
}

export interface GraphicsConsoleHandle {
  focus: () => void;
  sendCtrlAltDel: () => void;
  sendKeys: (keys: string) => void;
  sendKeyWithModifiers: (key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }) => void;
  sendKeyCode: (keyCode: number) => boolean;
  pasteText: (text: string) => Promise<void>;
  getSpiceConnection: () => any;
  screenshot: () => string | null;
  reconnect: () => void;
  disconnect: () => void;
}

// SPICE connection instance type
interface SpiceConnection {
  stop: () => void;
  sendCtrlAltDel?: () => void;
}

// WebSocket URL builder - uses NEXT_PUBLIC_API_URL so connections
// work through reverse proxies / tunnels (not just localhost:4050)
import { getGraphicsWsUrl } from "@/lib/ws-url";

// Correct script loading order for spice-html5
// Based on spice-html5-rails.js manifest
// IMPORTANT: main.js must be loaded AFTER all channel implementations
const SPICE_SCRIPTS = [
  // Core dependencies first
  'enums.js',
  'atKeynames.js',
  'utils.js',
  'png.js',
  'lz.js',
  'quic.js',
  'bitmap.js',
  // SPICE data handling
  'spicearraybuffer.js',
  'spicedataview.js',
  'spicetype.js',
  'spicemsg.js',
  'wire.js',
  'spiceconn.js',
  // Channel implementations (must be before main.js)
  'display.js',
  'inputs.js',
  'cursor.js',
  'simulatecursor.js',
  'playback.js',
  'port.js',
  // Third party crypto
  'thirdparty/jsbn.js',
  'thirdparty/rsa.js',
  'thirdparty/prng4.js',
  'thirdparty/rng.js',
  'thirdparty/sha1.js',
  // Authentication
  'ticket.js',
  // Main connection (uses all channel classes above)
  'main.js',
  // Additional features
  'filexfer.js',
  'resize.js',
  'webm.js',
];

// Track if SPICE is loading or loaded
let spiceLoadingPromise: Promise<void> | null = null;
let spiceLoaded = false;

// Load all SPICE scripts
async function loadSpiceScripts(): Promise<void> {
  if (spiceLoaded) return;
  
  if (spiceLoadingPromise) {
    return spiceLoadingPromise;
  }
  
  spiceLoadingPromise = (async () => {
    const basePath = '/spice-html5';
    
    // Load scripts sequentially in correct order
    for (const script of SPICE_SCRIPTS) {
      await new Promise<void>((resolve) => {
        // Check if already loaded
        const existingScript = document.querySelector(`script[src*="${script}"]`);
        if (existingScript) {
          resolve();
          return;
        }
        
        const el = document.createElement('script');
        el.src = `${basePath}/${script}`;
        el.async = false; // Important: maintain order
        
        el.onload = () => {
          resolve();
        };
        
        el.onerror = () => {
          console.warn(`[SPICE] Failed to load: ${script}`);
          resolve(); // Continue loading other scripts
        };
        
        document.head.appendChild(el);
      });
    }
    
    // Load CSS
    if (!document.querySelector('link[href*="spice.css"]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = `${basePath}/spice.css`;
      document.head.appendChild(link);
    }
    
    // Wait a bit for scripts to initialize
    await new Promise(r => setTimeout(r, 200));
    
    // Verify SpiceMainConn is available
    if (typeof (window as any).SpiceMainConn === 'undefined') {
      throw new Error('SpiceMainConn not available after loading scripts');
    }
    
    spiceLoaded = true;
  })();
  
  return spiceLoadingPromise;
}

export const GraphicsConsole = forwardRef<GraphicsConsoleHandle, GraphicsConsoleProps>(
  function GraphicsConsole(
    {
      consoleId,
      instanceName,
      host,
      project,
      width = 1024,
      height = 768,
      zoom = 100,
      onReady,
      onError,
      onClose,
      className,
    },
    ref
  ) {
    const containerRef = useRef<HTMLDivElement>(null);
    const screenRef = useRef<HTMLDivElement>(null);
    const spiceConnectionRef = useRef<SpiceConnection | null>(null);
    const [status, setStatus] = useState<'loading' | 'connecting' | 'connected' | 'error' | 'disconnected'>('loading');
    const [errorMessage, setErrorMessage] = useState<string>('');
    const [scriptsReady, setScriptsReady] = useState(false);
    const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
    
    // Connection attempt tracking
    const connectionAttemptRef = useRef(0);
    const isConnectingRef = useRef(false);
    // Track if connection is stabilized (after 500ms delay post-connect)
    const isStabilizedRef = useRef(false);
    const MAX_CONNECTION_ATTEMPTS = 3;

    const { updateConnectionStatus } = useConsoleStore();

    // Generate unique screen ID
    const screenId = `spice-screen-${consoleId}`;
    
    // Calculate scale factor for zoom
    const scale = zoom / 100;

    // Track canvas size changes for proper scaling container sizing
    useEffect(() => {
      if (status !== 'connected') return;
      
      const screen = document.getElementById(screenId);
      if (!screen) return;
      
      const updateCanvasSize = () => {
        const canvas = screen.querySelector('canvas') as HTMLCanvasElement;
        if (canvas) {
          // Get the native canvas dimensions (not CSS dimensions)
          const nativeWidth = canvas.width;
          const nativeHeight = canvas.height;
          if (nativeWidth > 0 && nativeHeight > 0) {
            setCanvasSize({ width: nativeWidth, height: nativeHeight });
          }
        }
      };
      
      // Initial check
      updateCanvasSize();
      
      // Use MutationObserver to detect when canvas is added or resized
      const observer = new MutationObserver(() => {
        updateCanvasSize();
      });
      
      observer.observe(screen, { childList: true, subtree: true, attributes: true });
      
      // Also check periodically for size changes (canvas might be resized by SPICE)
      const interval = setInterval(updateCanvasSize, 1000);
      
      return () => {
        observer.disconnect();
        clearInterval(interval);
      };
    }, [status, screenId]);

    // Apply zoom by scaling canvas CSS dimensions (preserves mouse coordinate accuracy)
    useEffect(() => {
      if (status !== 'connected' || canvasSize.width === 0) return;
      
      const screen = document.getElementById(screenId);
      if (!screen) return;
      
      const canvas = screen.querySelector('canvas') as HTMLCanvasElement;
      if (!canvas) return;
      
      // Set CSS dimensions to scaled size - browser handles mouse coord transformation
      canvas.style.width = `${canvasSize.width * scale}px`;
      canvas.style.height = `${canvasSize.height * scale}px`;
    }, [status, screenId, scale, canvasSize]);

    // DISABLED: Touch-to-mouse translation - replaced by OnScreenCursor feature
    // The OnScreenCursor provides better control for mobile users with explicit click modes
    /*
    // Touch event support for mobile - SPICE only listens for mouse events
    // We need to translate touch events to mouse events for mobile drag to work inside the console
    useEffect(() => {
      if (status !== 'connected') return;
      
      const screen = document.getElementById(screenId);
      if (!screen) return;
      
      // Canvas might not be available immediately after connection
      // Use MutationObserver to detect when canvas is added
      let canvas: HTMLCanvasElement | null = screen.querySelector('canvas');
      let cleanupFn: (() => void) | null = null;
      
      const setupTouchHandlers = (canvasEl: HTMLCanvasElement) => {
        // Track if we're currently in a touch sequence
        let isTouching = false;
        let lastTouchX = 0;
        let lastTouchY = 0;
      
        const handleTouchStart = (e: TouchEvent) => {
          if (e.touches.length !== 1) return; // Only handle single touch
          e.preventDefault(); // Prevent scrolling/zooming
          e.stopPropagation(); // Prevent parent handlers
          isTouching = true;
        
          const touch = e.touches[0];
          lastTouchX = touch.clientX;
          lastTouchY = touch.clientY;
          
          // Dispatch mouse event at touch position
          const mouseEvent = new MouseEvent('mousedown', {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: touch.clientX,
            clientY: touch.clientY,
            button: 0,
            buttons: 1,
          });
          canvasEl.dispatchEvent(mouseEvent);
        };
      
        const handleTouchMove = (e: TouchEvent) => {
          if (!isTouching || e.touches.length !== 1) return;
          e.preventDefault();
          e.stopPropagation();
        
          const touch = e.touches[0];
          lastTouchX = touch.clientX;
          lastTouchY = touch.clientY;
          
          const mouseEvent = new MouseEvent('mousemove', {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: touch.clientX,
            clientY: touch.clientY,
            button: 0,
            buttons: 1,
          });
          canvasEl.dispatchEvent(mouseEvent);
        };
      
        const handleTouchEnd = (e: TouchEvent) => {
          if (!isTouching) return;
          e.preventDefault();
          e.stopPropagation();
          isTouching = false;
        
          // Use last known position for end event
          const mouseEvent = new MouseEvent('mouseup', {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: lastTouchX,
            clientY: lastTouchY,
            button: 0,
            buttons: 0,
          });
          canvasEl.dispatchEvent(mouseEvent);
        };
      
        // Add touch listeners with passive: false to allow preventDefault
        canvasEl.addEventListener('touchstart', handleTouchStart, { passive: false, capture: true });
        canvasEl.addEventListener('touchmove', handleTouchMove, { passive: false, capture: true });
        canvasEl.addEventListener('touchend', handleTouchEnd, { passive: false, capture: true });
        canvasEl.addEventListener('touchcancel', handleTouchEnd, { passive: false, capture: true });
        
        return () => {
          canvasEl.removeEventListener('touchstart', handleTouchStart, { capture: true });
          canvasEl.removeEventListener('touchmove', handleTouchMove, { capture: true });
          canvasEl.removeEventListener('touchend', handleTouchEnd, { capture: true });
          canvasEl.removeEventListener('touchcancel', handleTouchEnd, { capture: true });
        };
      };
      
      if (canvas) {
        cleanupFn = setupTouchHandlers(canvas);
      } else {
        // Wait for canvas to be added
        const observer = new MutationObserver(() => {
          const newCanvas = screen.querySelector('canvas') as HTMLCanvasElement;
          if (newCanvas && !canvas) {
            canvas = newCanvas;
            cleanupFn = setupTouchHandlers(newCanvas);
            observer.disconnect();
          }
        });
        observer.observe(screen, { childList: true, subtree: true });
        
        return () => {
          observer.disconnect();
          cleanupFn?.();
        };
      }
      
      return () => {
        cleanupFn?.();
      };
    }, [status, screenId]);
    */

    // Block keyboard events on canvas during stabilization period
    // This intercepts events BEFORE SPICE's native handlers to prevent backtick spam
    useEffect(() => {
      if (status !== 'connected') return;
      
      const screen = document.getElementById(screenId);
      if (!screen) return;
      
      const canvas = screen.querySelector('canvas') as HTMLCanvasElement;
      if (!canvas) return;
      
      // Block ALL keyboard events until stabilized
      const blockKeyboard = (e: KeyboardEvent) => {
        if (!isStabilizedRef.current) {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          return false;
        }
      };
      
      // Capture phase to intercept before SPICE handlers
      canvas.addEventListener('keydown', blockKeyboard, { capture: true });
      canvas.addEventListener('keyup', blockKeyboard, { capture: true });
      canvas.addEventListener('keypress', blockKeyboard, { capture: true });
      
      return () => {
        canvas.removeEventListener('keydown', blockKeyboard, { capture: true });
        canvas.removeEventListener('keyup', blockKeyboard, { capture: true });
        canvas.removeEventListener('keypress', blockKeyboard, { capture: true });
      };
    }, [status, screenId]);

    // Window-level keyboard listener - captures keys even without focus on console
    // IMPORTANT: Only handles keys when console canvas is NOT focused
    // When canvas is focused, SPICE's native handlers process keyboard events
    // This prevents double key registration (especially noticeable in BIOS)
    useEffect(() => {
      if (status !== 'connected') return;
      
      // Add stabilization delay after connection to prevent spurious key events
      // This fixes backtick spam on console open
      let stabilized = false;
      const stabilizeTimer = setTimeout(() => {
        stabilized = true;
      }, 500);
      
      const handleWindowKeyDown = (e: KeyboardEvent) => {
        // Don't process until connection is stabilized
        if (!stabilized) return;
        
        // Get fresh reference to SPICE connection
        const sc = spiceConnectionRef.current as any;
        if (!sc || !sc.inputs || sc.inputs.state !== 'ready') return;
        
        // Skip if user is typing in an input field
        const target = e.target as HTMLElement;
        if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
          return;
        }
        
        // CRITICAL: Check if the SPICE screen or its canvas is the target
        // If so, SPICE's native handlers will process it - don't double-send
        const screen = document.getElementById(screenId);
        if (screen && (target === screen || screen.contains(target))) {
          // Canvas/screen is focused, SPICE handles it natively
          // Only prevent backtick spam
          if (e.repeat && (e.key === '`' || e.key === '~')) {
            e.preventDefault();
          }
          return;
        }
        
        // Prevent backtick key repeat spam
        if (e.repeat && (e.key === '`' || e.key === '~')) {
          e.preventDefault();
          return;
        }
        
        // Console is not focused, so SPICE won't receive this event
        // Send it via SPICE's handler manually
        const win = window as any;
        if (win.handle_keydown) {
          win.handle_keydown.call({ sc }, e);
        }
      };
      
      const handleWindowKeyUp = (e: KeyboardEvent) => {
        // Don't process until connection is stabilized
        if (!stabilized) return;
        
        const sc = spiceConnectionRef.current as any;
        if (!sc || !sc.inputs || sc.inputs.state !== 'ready') return;
        
        const target = e.target as HTMLElement;
        if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
          return;
        }
        
        // CRITICAL: Skip if SPICE screen is the target - native handlers will process
        const screen = document.getElementById(screenId);
        if (screen && (target === screen || screen.contains(target))) {
          return;
        }
        
        const win = window as any;
        if (win.handle_keyup) {
          win.handle_keyup.call({ sc }, e);
        }
      };
      
      // Capture phase to get keys before other handlers
      window.addEventListener('keydown', handleWindowKeyDown, true);
      window.addEventListener('keyup', handleWindowKeyUp, true);
      
      return () => {
        clearTimeout(stabilizeTimer);
        window.removeEventListener('keydown', handleWindowKeyDown, true);
        window.removeEventListener('keyup', handleWindowKeyUp, true);
      };
    }, [status, screenId]);

    // Load SPICE scripts on mount
    useEffect(() => {
      let mounted = true;
      
      loadSpiceScripts()
        .then(() => {
          if (mounted) {
            setScriptsReady(true);
          }
        })
        .catch((err) => {
          if (mounted) {
            console.error('[GraphicsConsole] Failed to load SPICE:', err);
            setStatus('error');
            setErrorMessage('Failed to load SPICE client');
          }
        });
      
      return () => {
        mounted = false;
      };
    }, []);

    // Connect to graphics console
    const connect = useCallback(() => {
      const screen = document.getElementById(screenId);
      if (!scriptsReady || !screen) {
        console.warn('[GraphicsConsole] Not ready - scriptsReady:', scriptsReady, 'screen:', !!screen);
        return;
      }
      
      // Prevent multiple simultaneous connection attempts
      if (isConnectingRef.current) {
        console.warn('[GraphicsConsole] Already connecting, skipping');
        return;
      }
      
      // Check connection attempt limit
      if (connectionAttemptRef.current >= MAX_CONNECTION_ATTEMPTS) {
        console.error('[GraphicsConsole] Max connection attempts reached');
        setStatus('error');
        setErrorMessage(`Connection failed after ${MAX_CONNECTION_ATTEMPTS} attempts. Click Reconnect to try again.`);
        updateConnectionStatus(consoleId, 'error', 'Max connection attempts reached');
        return;
      }
      
      connectionAttemptRef.current += 1;
      isConnectingRef.current = true;

      // Disconnect any existing connection
      if (spiceConnectionRef.current) {
        try {
          spiceConnectionRef.current.stop();
        } catch (e) {
          // Ignore
        }
        spiceConnectionRef.current = null;
      }

      setStatus('connecting');
      updateConnectionStatus(consoleId, 'connecting');
      setErrorMessage('');

      try {
        const wsUrl = getGraphicsWsUrl(instanceName, host, project);
        const SpiceMainConn = (window as any).SpiceMainConn;

        if (!SpiceMainConn) {
          isConnectingRef.current = false;
          throw new Error('SpiceMainConn not available');
        }

        // Reset stabilization flag - will be set after 500ms delay
        isStabilizedRef.current = false;

        // Create SPICE connection
        // LXD console doesn't require password (auth is done via LXD)
        const sc = new SpiceMainConn({
          uri: wsUrl,
          screen_id: screenId,
          password: '',  // LXD handles auth
          onerror: (e: Error | string) => {
            isConnectingRef.current = false;
            isStabilizedRef.current = false;
            let msg = typeof e === 'string' ? e : e?.message || 'Connection error';
            
            // Translate SPICE error messages to user-friendly messages
            if (msg.includes('Unexpected protocol mismatch')) {
              msg = 'Console connection failed. The VM may not be running or graphics console is unavailable.';
            } else if (msg.includes('Connection refused')) {
              msg = 'Cannot connect to console server. Please check if the backend is running.';
            } else if (msg.includes('Bad password')) {
              msg = 'Authentication failed for console session.';
            } else if (msg.includes('Permission denied')) {
              msg = 'Console session expired or access denied. Please reconnect.';
            }
            
            console.error('[GraphicsConsole] SPICE error:', msg);
            setStatus('error');
            setErrorMessage(msg);
            updateConnectionStatus(consoleId, 'error', msg);
            onError?.(msg);
          },
          onsuccess: () => {
            isConnectingRef.current = false;
            connectionAttemptRef.current = 0; // Reset on success
            setStatus('connected');
            updateConnectionStatus(consoleId, 'connected');
            
            // Set stabilization flag after 500ms to prevent backtick spam on first input
            setTimeout(() => {
              isStabilizedRef.current = true;
            }, 500);
            
            onReady?.();
          },
          onagent: () => {
            // SPICE agent connected
          },
        });

        spiceConnectionRef.current = sc;

      } catch (error) {
        isConnectingRef.current = false;
        const msg = error instanceof Error ? error.message : 'Failed to connect';
        console.error('[GraphicsConsole] Connection error:', error);
        setStatus('error');
        setErrorMessage(msg);
        updateConnectionStatus(consoleId, 'error', msg);
        onError?.(msg);
      }
    }, [consoleId, instanceName, host, project, scriptsReady, screenId, updateConnectionStatus, onReady, onError]);

    // Disconnect
    const disconnect = useCallback(() => {
      isConnectingRef.current = false;
      isStabilizedRef.current = false; // Reset stabilization on disconnect
      if (spiceConnectionRef.current) {
        try {
          spiceConnectionRef.current.stop();
        } catch (e) {
          // Ignore
        }
        spiceConnectionRef.current = null;
      }
      setStatus('disconnected');
      updateConnectionStatus(consoleId, 'disconnected');
    }, [consoleId, updateConnectionStatus]);

    // Reconnect - resets attempt counter for manual reconnect
    const reconnect = useCallback(() => {
      connectionAttemptRef.current = 0; // Reset counter on manual reconnect
      disconnect();
      setTimeout(() => connect(), 200);
    }, [connect, disconnect]);

    // important: guarded. Both overlay buttons below call this, and repeated
    // clicks used to stack disconnect/connect cycles on top of each other.
    const { pending: reconnectPending, run: runReconnect } = useManualRefresh(
      reconnect,
      { errorTitle: "Reconnect failed" },
    );

    // Helper to send key via SPICE input channel using scancodes
    // This directly sends to the SPICE connection, bypassing DOM events
    const sendSpiceKey = useCallback((keyCode: number, press: boolean = true, release: boolean = true) => {
      const sc = spiceConnectionRef.current as any;
      if (!sc || !sc.inputs || sc.inputs.state !== 'ready') return false;
      
      // Don't send keys until connection is stabilized (prevents backtick spam on open)
      if (!isStabilizedRef.current) {
        return false;
      }
      
      const win = window as any;
      if (!win.keycode_to_start_scan || !win.SpiceMsgcKeyDown || !win.SpiceMiniData) {
        console.warn('[GraphicsConsole] SPICE functions not available');
        return false;
      }
      
      try {
        if (press) {
          const keyDown = new win.SpiceMsgcKeyDown();
          keyDown.code = win.keycode_to_start_scan(keyCode);
          const msgDown = new win.SpiceMiniData();
          msgDown.build_msg(win.SPICE_MSGC_INPUTS_KEY_DOWN, keyDown);
          sc.inputs.send_msg(msgDown);
        }
        
        if (release) {
          const keyUp = new win.SpiceMsgcKeyUp();
          keyUp.code = win.keycode_to_end_scan(keyCode);
          const msgUp = new win.SpiceMiniData();
          msgUp.build_msg(win.SPICE_MSGC_INPUTS_KEY_UP, keyUp);
          sc.inputs.send_msg(msgUp);
        }
        
        return true;
      } catch (e) {
        console.error('[GraphicsConsole] Failed to send key:', e);
        return false;
      }
    }, []);

    // Expose methods via ref
    useImperativeHandle(ref, () => ({
      focus: () => {
        const screen = document.getElementById(screenId);
        screen?.focus();
        const canvas = screen?.querySelector('canvas');
        if (canvas) (canvas as HTMLCanvasElement).focus();
      },
      sendCtrlAltDel: () => {
        const sc = spiceConnectionRef.current;
        if (typeof (window as any).sendCtrlAltDel === 'function') {
          (window as any).sendCtrlAltDel(sc);
        }
      },
      sendKeys: (keys: string) => {
        // For Ctrl+Alt+Del, use the special function
        if (keys.toLowerCase() === 'ctrl+alt+del') {
          const sc = spiceConnectionRef.current;
          if (typeof (window as any).sendCtrlAltDel === 'function') {
            (window as any).sendCtrlAltDel(sc);
          }
          return;
        }
        
        // Complete map of key names to JavaScript keyCodes for SPICE scancode mapping
        // This includes special keys, function keys, and character keyCodes
        const keyCodeMap: Record<string, number> = {
          // Special keys
          'Escape': 27, 'Esc': 27,
          'Enter': 13, 'Tab': 9, 'Backspace': 8, 'Space': 32, ' ': 32,
          'Delete': 46, 'Del': 46, 'Insert': 45, 'Ins': 45,
          'Home': 36, 'End': 35, 'PageUp': 33, 'PageDown': 34,
          'ArrowUp': 38, 'ArrowDown': 40, 'ArrowLeft': 37, 'ArrowRight': 39,
          'Shift': 16, 'Control': 17, 'Ctrl': 17, 'Alt': 18, 'Meta': 91,
          'CapsLock': 20, 'NumLock': 144, 'ScrollLock': 145,
          'PrintScreen': 44, 'Pause': 19,
          // Function keys
          'F1': 112, 'F2': 113, 'F3': 114, 'F4': 115,
          'F5': 116, 'F6': 117, 'F7': 118, 'F8': 119,
          'F9': 120, 'F10': 121, 'F11': 122, 'F12': 123,
          // Punctuation and symbols (using DOM keyCodes that SPICE understands)
          ';': 186, ':': 186, // semicolon (needs shift for colon)
          '=': 187, '+': 187, // equals (needs shift for plus)
          ',': 188, '<': 188, // comma (needs shift for less-than)
          '-': 189, '_': 189, // minus (needs shift for underscore)
          '.': 190, '>': 190, // period (needs shift for greater-than)
          '/': 191, '?': 191, // slash (needs shift for question)
          '`': 192, '~': 192, // backtick (needs shift for tilde)
          '[': 219, '{': 219, // left bracket (needs shift for brace)
          '\\': 220, '|': 220, // backslash (needs shift for pipe)
          ']': 221, '}': 221, // right bracket (needs shift for brace)
          "'": 222, '"': 222, // quote (needs shift for double-quote)
        };
        
        // Characters that need shift to be typed
        const shiftChars = new Set(['!', '@', '#', '$', '%', '^', '&', '*', '(', ')', '_', '+', '{', '}', '|', ':', '"', '<', '>', '?', '~']);
        
        // Number keys with shift for symbols
        const numShiftMap: Record<string, number> = {
          '!': 49, '@': 50, '#': 51, '$': 52, '%': 53, 
          '^': 54, '&': 55, '*': 56, '(': 57, ')': 48,
        };
        
        // If it's a named key, send via SPICE channel
        if (keyCodeMap[keys]) {
          sendSpiceKey(keyCodeMap[keys]);
          return;
        }
        
        // For regular characters, send each one
        for (const char of keys) {
          let keyCode: number;
          let needsShift = false;
          
          // Check if it's a shift+number symbol
          if (numShiftMap[char]) {
            keyCode = numShiftMap[char];
            needsShift = true;
          }
          // Check if it's in keyCodeMap (punctuation)
          else if (keyCodeMap[char]) {
            keyCode = keyCodeMap[char];
            needsShift = shiftChars.has(char);
          }
          // Letter keys
          else if (/[a-zA-Z]/.test(char)) {
            keyCode = char.toUpperCase().charCodeAt(0);
            needsShift = char === char.toUpperCase() && char !== char.toLowerCase();
          }
          // Number keys
          else if (/[0-9]/.test(char)) {
            keyCode = char.charCodeAt(0);
          }
          // Fallback
          else {
            keyCode = char.toUpperCase().charCodeAt(0);
          }
          
          // Send with shift if needed
          if (needsShift) {
            sendSpiceKey(16, true, false); // Shift down
          }
          sendSpiceKey(keyCode);
          if (needsShift) {
            sendSpiceKey(16, false, true); // Shift up
          }
        }
      },
      // Send a key with explicit modifiers (Ctrl, Alt, Shift, Meta)
      sendKeyWithModifiers: (key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }) => {
        // Map key names to JavaScript keyCodes
        const keyCodeMap: Record<string, number> = {
          // Special keys
          'Escape': 27, 'Esc': 27,
          'Enter': 13, 'Tab': 9, 'Backspace': 8, 'Space': 32, ' ': 32,
          'Delete': 46, 'Del': 46, 'Insert': 45, 'Ins': 45,
          'Home': 36, 'End': 35, 'PageUp': 33, 'PageDown': 34,
          'ArrowUp': 38, 'ArrowDown': 40, 'ArrowLeft': 37, 'ArrowRight': 39,
          'Shift': 16, 'Control': 17, 'Ctrl': 17, 'Alt': 18, 'Meta': 91,
          'CapsLock': 20, 'NumLock': 144, 'ScrollLock': 145,
          'PrintScreen': 44, 'Pause': 19,
          // Function keys
          'F1': 112, 'F2': 113, 'F3': 114, 'F4': 115,
          'F5': 116, 'F6': 117, 'F7': 118, 'F8': 119,
          'F9': 120, 'F10': 121, 'F11': 122, 'F12': 123,
          // Punctuation
          ';': 186, ':': 186, '=': 187, '+': 187, ',': 188, '<': 188,
          '-': 189, '_': 189, '.': 190, '>': 190, '/': 191, '?': 191,
          '`': 192, '~': 192, '[': 219, '{': 219, '\\': 220, '|': 220,
          ']': 221, '}': 221, "'": 222, '"': 222,
        };
        
        // Symbols that require shift on US keyboard layout
        const shiftSymbols = new Set(['!', '@', '#', '$', '%', '^', '&', '*', '(', ')', '_', '+', '{', '}', '|', ':', '"', '<', '>', '?', '~']);
        
        // Number key shift map
        const numShiftMap: Record<string, number> = {
          '!': 49, '@': 50, '#': 51, '$': 52, '%': 53,
          '^': 54, '&': 55, '*': 56, '(': 57, ')': 48,
        };
        
        // Determine keyCode and if shift is needed for this character
        let keyCode: number;
        let needsShift = modifiers?.shift || false;
        
        if (numShiftMap[key]) {
          keyCode = numShiftMap[key];
          needsShift = true;
        } else if (keyCodeMap[key]) {
          keyCode = keyCodeMap[key];
          // Check if this symbol needs shift
          if (shiftSymbols.has(key)) needsShift = true;
        } else if (/[a-zA-Z]/.test(key) && key.length === 1) {
          keyCode = key.toUpperCase().charCodeAt(0);
          // Check if uppercase letter (needs shift)
          if (key === key.toUpperCase() && key !== key.toLowerCase()) needsShift = true;
        } else if (/[0-9]/.test(key)) {
          keyCode = key.charCodeAt(0);
        } else {
          keyCode = key.toUpperCase().charCodeAt(0);
        }
        
        // Press modifier keys
        if (needsShift) sendSpiceKey(16, true, false);  // Shift down
        if (modifiers?.ctrl) sendSpiceKey(17, true, false);   // Ctrl down
        if (modifiers?.alt) sendSpiceKey(18, true, false);    // Alt down
        if (modifiers?.meta) sendSpiceKey(91, true, false);   // Meta/Win down
        
        // Send the key
        sendSpiceKey(keyCode);
        
        // Release modifier keys (reverse order)
        if (modifiers?.meta) sendSpiceKey(91, false, true);   // Meta/Win up
        if (modifiers?.alt) sendSpiceKey(18, false, true);    // Alt up
        if (modifiers?.ctrl) sendSpiceKey(17, false, true);   // Ctrl up
        if (needsShift) sendSpiceKey(16, false, true);  // Shift up
      },
      // Send a single key by JavaScript keyCode (for BIOS spam, etc.)
      sendKeyCode: (keyCode: number) => {
        return sendSpiceKey(keyCode);
      },
      pasteText: async (text: string) => {
        // Complete keyCode mapping for all printable ASCII characters
        const charToKeyCode: Record<string, { keyCode: number; shift: boolean }> = {
          // Numbers (no shift)
          '0': { keyCode: 48, shift: false }, '1': { keyCode: 49, shift: false },
          '2': { keyCode: 50, shift: false }, '3': { keyCode: 51, shift: false },
          '4': { keyCode: 52, shift: false }, '5': { keyCode: 53, shift: false },
          '6': { keyCode: 54, shift: false }, '7': { keyCode: 55, shift: false },
          '8': { keyCode: 56, shift: false }, '9': { keyCode: 57, shift: false },
          // Shifted number row symbols
          ')': { keyCode: 48, shift: true }, '!': { keyCode: 49, shift: true },
          '@': { keyCode: 50, shift: true }, '#': { keyCode: 51, shift: true },
          '$': { keyCode: 52, shift: true }, '%': { keyCode: 53, shift: true },
          '^': { keyCode: 54, shift: true }, '&': { keyCode: 55, shift: true },
          '*': { keyCode: 56, shift: true }, '(': { keyCode: 57, shift: true },
          // Punctuation keys
          ';': { keyCode: 186, shift: false }, ':': { keyCode: 186, shift: true },
          '=': { keyCode: 187, shift: false }, '+': { keyCode: 187, shift: true },
          ',': { keyCode: 188, shift: false }, '<': { keyCode: 188, shift: true },
          '-': { keyCode: 189, shift: false }, '_': { keyCode: 189, shift: true },
          '.': { keyCode: 190, shift: false }, '>': { keyCode: 190, shift: true },
          '/': { keyCode: 191, shift: false }, '?': { keyCode: 191, shift: true },
          '`': { keyCode: 192, shift: false }, '~': { keyCode: 192, shift: true },
          '[': { keyCode: 219, shift: false }, '{': { keyCode: 219, shift: true },
          '\\': { keyCode: 220, shift: false }, '|': { keyCode: 220, shift: true },
          ']': { keyCode: 221, shift: false }, '}': { keyCode: 221, shift: true },
          "'": { keyCode: 222, shift: false }, '"': { keyCode: 222, shift: true },
          // Whitespace
          ' ': { keyCode: 32, shift: false },
          '\t': { keyCode: 9, shift: false },
          '\n': { keyCode: 13, shift: false },
          '\r': { keyCode: 13, shift: false },
        };
        
        // Generate letter mappings (a-z, A-Z)
        for (let i = 0; i < 26; i++) {
          const lower = String.fromCharCode(97 + i); // a-z
          const upper = String.fromCharCode(65 + i); // A-Z
          const keyCode = 65 + i; // A=65, B=66, etc.
          charToKeyCode[lower] = { keyCode, shift: false };
          charToKeyCode[upper] = { keyCode, shift: true };
        }
        
        // Paste text by sending keys via SPICE input channel
        for (let i = 0; i < text.length; i++) {
          const char = text[i];
          const mapping = charToKeyCode[char];
          
          if (mapping) {
            if (mapping.shift) {
              sendSpiceKey(16, true, false); // Shift down
            }
            sendSpiceKey(mapping.keyCode);
            if (mapping.shift) {
              sendSpiceKey(16, false, true); // Shift up
            }
          } else {
            // For unmapped characters, try using charCode directly
            // This may not work for all characters but is a fallback
            console.warn(`[pasteText] No mapping for character: '${char}' (code: ${char.charCodeAt(0)})`);
          }
          
          // Small delay between characters for reliability
          if (i < text.length - 1) {
            await new Promise(r => setTimeout(r, 15));
          }
        }
      },
      // Get the spice connection reference for direct access
      getSpiceConnection: () => spiceConnectionRef.current,
      screenshot: () => {
        const screen = document.getElementById(screenId);
        const canvas = screen?.querySelector('canvas');
        if (canvas) {
          try {
            return canvas.toDataURL('image/png');
          } catch {
            return null;
          }
        }
        return null;
      },
      reconnect,
      disconnect,
    }), [screenId, reconnect, disconnect]);

    // Connect when scripts are ready - with robust DOM check for mobile
    useEffect(() => {
      if (!scriptsReady || status !== 'loading') return;
      
      // Check for screen element with retry for mobile devices
      // On mobile, the DOM might not be ready immediately due to different rendering timing
      let connectAttempted = false;
      let retryCount = 0;
      const maxRetries = 10; // Maximum 10 retries (1 second total)
      
      const attemptConnect = () => {
        if (connectAttempted || status !== 'loading') return;
        
        const screenEl = document.getElementById(screenId);
        if (screenEl) {
          connectAttempted = true;
          // Use requestAnimationFrame to ensure DOM is painted
          requestAnimationFrame(() => {
            if (status === 'loading') {
              connect();
            }
          });
        } else if (retryCount < maxRetries) {
          retryCount++;
          // Retry with exponential backoff (100ms, 100ms, 100ms... up to 1 second)
          setTimeout(attemptConnect, 100);
        } else {
          console.error('[GraphicsConsole] Screen element not found after retries');
        }
      };
      
      // Start connection attempt
      attemptConnect();
      
      return () => {
        connectAttempted = true; // Prevent connection after unmount
      };
    }, [scriptsReady, status, connect, screenId]);

    // Cleanup on unmount
    useEffect(() => {
      return () => {
        disconnect();
      };
    }, [disconnect]);

    // Overlay component for status display
    const StatusOverlay = () => {
      if (status === 'loading') {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <Loader2 className="h-8 w-8 animate-spin text-white/70" />
            <p className="text-sm text-white/70">Loading SPICE client...</p>
          </div>
        );
      }

      if (status === 'error') {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <AlertCircle className="h-8 w-8 text-red-400" />
            <p className="text-sm text-white/70">{errorMessage || 'Connection failed'}</p>
            <Button
              aria-busy={reconnectPending}
              variant="outline"
              size="sm"
              disabled={reconnectPending}
              onClick={() => void runReconnect()}
              className="text-white border-white/30 hover:bg-white/10"
            >
              <RefreshCw
                className={cn(
                  "h-4 w-4 mr-2",
                  reconnectPending && "animate-spin motion-reduce:animate-none",
                )}
              />
              Reconnect
            </Button>
          </div>
        );
      }

      if (status === 'connecting') {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <div className="relative">
              <Monitor className="h-12 w-12 text-white/70" />
              <Wifi className="h-4 w-4 text-blue-400 absolute -top-1 -right-1 animate-pulse" />
            </div>
            <p className="text-sm text-white/70">Connecting to {instanceName}...</p>
            <p className="text-xs text-white/50">Establishing SPICE connection</p>
          </div>
        );
      }

      if (status === 'disconnected') {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <div className="relative">
              <Monitor className="h-12 w-12 text-white/50" />
              <WifiOff className="h-4 w-4 text-white/50 absolute -top-1 -right-1" />
            </div>
            <p className="text-sm text-white/70">Disconnected from {instanceName}</p>
            <Button
              aria-busy={reconnectPending}
              variant="outline"
              size="sm"
              disabled={reconnectPending}
              onClick={() => void runReconnect()}
              className="text-white border-white/30 hover:bg-white/10"
            >
              <RefreshCw
                className={cn(
                  "h-4 w-4 mr-2",
                  reconnectPending && "animate-spin motion-reduce:animate-none",
                )}
              />
              Reconnect
            </Button>
          </div>
        );
      }

      return null; // Connected - no overlay
    };

    // Always render screen div, overlay status on top
    // Mouse coordinates are calculated using getBoundingClientRect in modified spicemsg.js
    return (
      <div
        ref={containerRef}
        className={cn("w-full h-full bg-black relative", className)}
        style={{
          overflow: 'auto', // Allow scrolling if canvas is larger than container
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'flex-start',
        }}
        onClick={(e) => {
          // Focus the screen on click for keyboard input
          const screen = document.getElementById(screenId);
          if (screen) {
            screen.focus();
            const canvas = screen.querySelector('canvas');
            if (canvas) {
              (canvas as HTMLCanvasElement).focus();
            }
          }
        }}
      >
        {/* Status overlay - positioned absolutely over everything */}
        <StatusOverlay />
        
        {/* SPICE screen container - canvas rendered by spice-html5 */}
        <div
          ref={screenRef}
          id={screenId}
          className="spice-screen"
          style={{
            position: 'relative',
            visibility: status === 'connected' ? 'visible' : 'hidden',
            outline: 'none',
            touchAction: 'none', // Disable browser touch gestures, let SPICE handle
          }}
          tabIndex={0}
          onMouseDown={(e) => {
            // Stop propagation to prevent draggable from capturing mouse events
            e.stopPropagation();
          }}
          onTouchStart={(e) => {
            // Stop propagation to prevent draggable from capturing touch events
            // This fixes mobile drag issue - allows dragging inside console (file manager, etc.)
            e.stopPropagation();
          }}
          onTouchMove={(e) => {
            // Allow touch move inside console without moving the window
            e.stopPropagation();
          }}
          onPointerDown={(e) => {
            // Stop pointer events from propagating to draggable
            e.stopPropagation();
          }}
          onClick={(e) => {
            e.stopPropagation();
            const canvas = e.currentTarget.querySelector('canvas');
            if (canvas) {
              (canvas as HTMLCanvasElement).focus();
            }
          }}
        />
        
        {/* SPICE canvas styling */}
        <style jsx global>{`
          .spice-screen {
            pointer-events: auto;
          }
          .spice-screen canvas {
            display: block;
            outline: none;
            cursor: default;
            pointer-events: auto;
          }
          .spice-screen canvas:focus {
            outline: none;
          }
        `}</style>
      </div>
    );
  }
);

export default GraphicsConsole;
