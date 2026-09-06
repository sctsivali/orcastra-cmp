/**
 * Expanded Console Page - Full-page console view for VMs and containers.
 * Accessible via /console/[id] route.
 * 
 * Supports both:
 * - Text console (xterm.js) - for containers and VMs
 * - Graphics console (SPICE) - for VMs only
 */

"use client";

import * as React from "react";
import { Suspense, useEffect, useState, useRef, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import {
  Terminal as TerminalIcon,
  ArrowLeft,
  Monitor,
  Search,
  RefreshCw,
  Loader2,
  CheckCircle2,
  AlertCircle,
  X,
  Play,
  Square,
  RotateCcw,
  AlertTriangle,
  Keyboard,
  ClipboardList,
  ZoomIn,
  ZoomOut,
  Rocket,
  ChevronDown,
  MousePointer2,
} from "lucide-react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { SearchAddon } from "@xterm/addon-search";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Tabs,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import {
  FORCE_OFF_HINT,
  FORCE_ON_HINT,
  FORCE_OPTION_LABEL,
} from "@/lib/instance-action-copy";
import { useManualRefresh } from "@/hooks/use-manual-refresh";
import { API_URL, getAccessToken } from "@/lib/api";
import { AgentActivityBadge } from "./components/agent-activity-badge";
import { OnScreenKeyboard } from "@/components/console/on-screen-keyboard";
import { OnScreenCursor } from "@/components/console/on-screen-cursor";
import { ClipboardManager } from "@/components/console/clipboard-manager";
import { ConsoleResourceBar } from "@/components/console/resource-bar";
import "@xterm/xterm/css/xterm.css";
import { TERMINAL_THEME } from "@/lib/terminal-theme";

type ConsoleStatus = "loading" | "connecting" | "connected" | "disconnected" | "error";
type InstanceStatus = "Running" | "Stopped" | "Starting" | "Stopping" | "Restarting" | "Unknown" | "Error";
type OperationType = "start" | "stop" | "restart";
type ConsoleType = "text" | "graphics";

// SPICE script loader - order is critical, main.js must be last
const SPICE_SCRIPTS = [
  "enums.js", "atKeynames.js", "utils.js", "png.js", "lz.js", "quic.js", "bitmap.js",
  "spicearraybuffer.js", "spicedataview.js", "spicetype.js", "spicemsg.js", "wire.js",
  "spiceconn.js", "display.js", "inputs.js", "cursor.js", "simulatecursor.js",
  "playback.js", "port.js",
  "thirdparty/jsbn.js", "thirdparty/rsa.js", "thirdparty/prng4.js", "thirdparty/rng.js",
  "thirdparty/sha1.js", "ticket.js",
  "main.js",  // Must be after all channel implementations
  "filexfer.js", "resize.js", "webm.js",
];

let spiceLoadingPromise: Promise<void> | null = null;
let spiceLoaded = false;

async function loadSpiceScripts(): Promise<void> {
  if (spiceLoaded) return;
  if (spiceLoadingPromise) return spiceLoadingPromise;

  spiceLoadingPromise = (async () => {
    const basePath = "/spice-html5";
    for (const script of SPICE_SCRIPTS) {
      await new Promise<void>((resolve) => {
        const existingScript = document.querySelector(`script[src*="${script}"]`);
        if (existingScript) { resolve(); return; }
        const el = document.createElement("script");
        el.src = `${basePath}/${script}`;
        el.async = false;
        el.onload = () => resolve();
        el.onerror = () => { console.warn(`[SPICE] Failed to load: ${script}`); resolve(); };
        document.head.appendChild(el);
      });
    }
    if (!document.querySelector('link[href*="spice.css"]')) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = `${basePath}/spice.css`;
      document.head.appendChild(link);
    }
    await new Promise((r) => setTimeout(r, 200));
    if (typeof (window as any).SpiceMainConn === "undefined") {
      throw new Error("SpiceMainConn not available");
    }
    spiceLoaded = true;
  })();

  return spiceLoadingPromise;
}

// WebSocket URL builders - shared utility for proxy-safe URLs
import { getTerminalWsUrl, getGraphicsWsUrl } from "@/lib/ws-url";

// Loading fallback
function ConsoleLoading() {
  return (
    <div className="flex items-center justify-center h-screen bg-terminal">
      <div className="flex flex-col items-center gap-4">
        <Loader2 className="h-8 w-8 animate-spin text-blue-400" />
        <span className="text-gray-400">Loading console...</span>
      </div>
    </div>
  );
}

// Main console content
function ConsoleContent() {
  const params = useParams();
  const searchParams = useSearchParams();

  const instanceName = searchParams.get("instance") || "";
  const host = searchParams.get("host") || "";
  const project = searchParams.get("project") || "default";
  const instanceType = searchParams.get("type") || "virtual-machine";
  const initialTab = (searchParams.get("tab") as ConsoleType) || "graphics";
  // continuation=1 means this is opened from window console, skip session check
  const isContinuation = searchParams.get("continuation") === "1";

  const isVM = instanceType === "virtual-machine";
  const [activeTab, setActiveTab] = useState<ConsoleType>(isVM ? initialTab : "text");

  // Text console refs
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const searchAddonRef = useRef<SearchAddon | null>(null);
  const textWsRef = useRef<WebSocket | null>(null);
  const textInitializingRef = useRef(false); // Prevent double initialization

  // Graphics console refs
  const graphicsRef = useRef<HTMLDivElement>(null);
  const spiceRef = useRef<any>(null);
  const graphicsConnectingRef = useRef(false); // Prevent double connection

  // Session conflict state
  interface ActiveSession {
    operation_id: string;
    instance_name: string;
    description: string;
    created_at: string;
    may_cancel: boolean;
  }
  const [sessionConflict, setSessionConflict] = useState<{
    show: boolean;
    sessions: ActiveSession[];
  }>({ show: false, sessions: [] });
  const [sessionCheckComplete, setSessionCheckComplete] = useState(isContinuation); // Skip check if continuation
  const [killingSession, setKillingSession] = useState(false);

  // State
  const [textStatus, setTextStatus] = useState<ConsoleStatus>("disconnected");
  const [graphicsStatus, setGraphicsStatus] = useState<ConsoleStatus>("loading");
  const [instanceStatus, setInstanceStatus] = useState<InstanceStatus>("Running");
  const [sessionId, setSessionId] = useState<string>("");
  const [showSearch, setShowSearch] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [error, setError] = useState<string>("");
  const [operationLoading, setOperationLoading] = useState<OperationType | null>(null);
  const [spiceReady, setSpiceReady] = useState(false);
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    operation: OperationType | null;
    title: string;
    description: string;
    // Power-cycle instead of requesting a graceful guest shutdown.
    force: boolean;
  }>({ open: false, operation: null, title: "", description: "", force: false });
  
  // New feature states
  const [showKeyboard, setShowKeyboard] = useState(false);
  const [showCursor, setShowCursor] = useState(false);
  const [showClipboard, setShowClipboard] = useState(false);
  const [zoom, setZoom] = useState(100);
  const [biosSpamming, setBiosSpamming] = useState(false);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  // Track if SPICE connection is stabilized (after 500ms delay post-connect)
  const isStabilizedRef = useRef(false);

  const screenId = `spice-screen-${params.id}`;
  const scale = zoom / 100;
  
  // BIOS boot key options - now with keyCodes for SPICE
  const BIOS_KEYS = [
    { key: 'Delete', label: 'Del', description: 'Most BIOS (Dell, ASUS, MSI)', keyCode: 46 },
    { key: 'F2', label: 'F2', description: 'UEFI/BIOS (Lenovo, Acer, ASUS)', keyCode: 113 },
    { key: 'F12', label: 'F12', description: 'Boot Menu (Dell, Lenovo)', keyCode: 123 },
    { key: 'Escape', label: 'Esc', description: 'Boot Menu (HP)', keyCode: 27 },
    { key: 'F10', label: 'F10', description: 'BIOS (HP, Compaq)', keyCode: 121 },
    { key: 'F1', label: 'F1', description: 'BIOS (Lenovo ThinkPad)', keyCode: 112 },
  ];

  // Helper to send key via SPICE input channel using scancodes
  // This directly sends to the SPICE connection, bypassing DOM events
  const sendSpiceKey = useCallback((keyCode: number, press: boolean = true, release: boolean = true): boolean => {
    const sc = spiceRef.current as any;
    if (!sc || !sc.inputs || sc.inputs.state !== 'ready') return false;
    
    // Don't send keys until connection is stabilized (prevents backtick spam on open)
    if (!isStabilizedRef.current) {
      return false;
    }
    
    const win = window as any;
    if (!win.keycode_to_start_scan || !win.SpiceMsgcKeyDown || !win.SpiceMiniData) {
      console.warn('[Console] SPICE functions not available');
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
      console.error('[Console] Failed to send key:', e);
      return false;
    }
  }, []);

  // Check for active sessions before connecting (unless continuation)
  useEffect(() => {
    if (isContinuation || !instanceName || !host) {
      setSessionCheckComplete(true);
      return;
    }

    const checkActiveSessions = async () => {
      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(instanceName)}/active-sessions` +
            `?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`,
          {
            headers: {
              ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
            },
          }
        );
        if (response.ok) {
          const data = await response.json();
          if (data.has_active_session && data.sessions.length > 0) {
            setSessionConflict({ show: true, sessions: data.sessions });
          }
        }
      } catch (err) {
        console.warn("[Console] Failed to check active sessions:", err);
      } finally {
        setSessionCheckComplete(true);
      }
    };

    checkActiveSessions();
  }, [instanceName, host, project, isContinuation]);

  // Kill existing session
  const handleKillSession = async (operationId: string) => {
    setKillingSession(true);
    try {
      const response = await fetch(
        `${API_URL}/console/${encodeURIComponent(instanceName)}/active-sessions/${operationId}` +
          `?host=${encodeURIComponent(host)}`,
        {
          method: "DELETE",
          headers: {
            ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
          },
        }
      );
      if (response.ok) {
        setSessionConflict((prev) => ({
          ...prev,
          sessions: prev.sessions.filter((s) => s.operation_id !== operationId),
          show: prev.sessions.length > 1,
        }));
      }
    } catch (err) {
      console.error("[Console] Failed to kill session:", err);
    } finally {
      setKillingSession(false);
    }
  };

  // Load SPICE scripts for VMs
  useEffect(() => {
    if (!isVM) return;
    loadSpiceScripts()
      .then(() => setSpiceReady(true))
      .catch((err) => {
        console.error("[Console] Failed to load SPICE:", err);
        setGraphicsStatus("error");
      });
  }, [isVM]);

  // Fetch initial instance status from capabilities API
  // This ensures Start/Stop buttons have correct initial state
  useEffect(() => {
    if (!instanceName || !host) return;
    
    const fetchInstanceStatus = async () => {
      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(instanceName)}/capabilities` +
            `?host=${encodeURIComponent(host)}` +
            `&project=${encodeURIComponent(project)}`,
          {
            headers: {
              ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
            },
          }
        );
        
        if (response.ok) {
          const data = await response.json();
          const state = data.state ?? 'Unknown';
          if (state && state !== 'Unknown') {
            setInstanceStatus(state as InstanceStatus);
          }
        }
      } catch (err) {
        console.warn("[Console] Failed to fetch instance status:", err);
        // Keep default status on error
      }
    };
    
    fetchInstanceStatus();
  }, [instanceName, host, project]);

  // Initialize text terminal
  const initTextTerminal = useCallback(() => {
    // Prevent double initialization
    if (!terminalRef.current || xtermRef.current || textInitializingRef.current) return;
    textInitializingRef.current = true;

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "block",
      fontSize: 15,
      fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", Consolas, monospace',
      theme: TERMINAL_THEME,
      allowProposedApi: true,
      scrollback: 50000,
      convertEol: true,
    });

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();
    const searchAddon = new SearchAddon();

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);
    term.loadAddon(searchAddon);

    term.open(terminalRef.current);
    fitAddon.fit();

    xtermRef.current = term;
    fitAddonRef.current = fitAddon;
    searchAddonRef.current = searchAddon;

    term.onData((data) => {
      if (textWsRef.current?.readyState === WebSocket.OPEN) {
        textWsRef.current.send(JSON.stringify({ type: "input", data }));
      }
    });

    term.onResize(({ cols, rows }) => {
      if (textWsRef.current?.readyState === WebSocket.OPEN) {
        textWsRef.current.send(JSON.stringify({ type: "resize", width: cols, height: rows }));
      }
    });

    term.writeln("\x1b[1;34m╔═══════════════════════════════════════════════════════════════╗\x1b[0m");
    term.writeln("\x1b[1;34m║\x1b[0m  \x1b[1;36mOrcastra Console - Full View\x1b[0m                                \x1b[1;34m║\x1b[0m");
    term.writeln("\x1b[1;34m║\x1b[0m  Connecting to \x1b[1;33m" + instanceName.padEnd(20) + "\x1b[0m                         \x1b[1;34m║\x1b[0m");
    term.writeln("\x1b[1;34m╚═══════════════════════════════════════════════════════════════╝\x1b[0m");
    term.writeln("");

    return term;
  }, [instanceName]);

  // Connect text console
  const connectText = useCallback(() => {
    if (!instanceName || !host) return;
    // Prevent reconnect if already connected or connecting
    if (textWsRef.current?.readyState === WebSocket.OPEN || 
        textWsRef.current?.readyState === WebSocket.CONNECTING) {
      return;
    }

    const wsUrl = getTerminalWsUrl(instanceName, host, project);

    setTextStatus("connecting");
    const ws = new WebSocket(wsUrl);
    textWsRef.current = ws;

    ws.onopen = () => {
      if (xtermRef.current) {
        const { cols, rows } = xtermRef.current;
        ws.send(JSON.stringify({ width: cols, height: rows }));
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        switch (msg.type) {
          case "session":
            setSessionId(msg.session_id);
            xtermRef.current?.writeln(`\x1b[1;32m✓ Session: ${msg.session_id}\x1b[0m`);
            break;
          case "ready":
            setTextStatus("connected");
            xtermRef.current?.writeln("\x1b[1;32m✓ Terminal ready\x1b[0m\n");
            break;
          case "output":
            if (msg.encoding === "base64") {
              xtermRef.current?.write(atob(msg.data));
            } else {
              xtermRef.current?.write(msg.data);
            }
            break;
          case "error":
            xtermRef.current?.writeln(`\x1b[1;31m✗ Error: ${msg.message}\x1b[0m`);
            setTextStatus("error");
            setError(msg.message);
            break;
          case "closed":
            xtermRef.current?.writeln("\n\x1b[1;33m⚠ Connection closed\x1b[0m");
            setTextStatus("disconnected");
            break;
        }
      } catch (e) {
        xtermRef.current?.write(event.data);
      }
    };

    ws.onerror = () => {
      xtermRef.current?.writeln("\x1b[1;31m✗ Connection error\x1b[0m");
      setTextStatus("error");
    };

    ws.onclose = () => {
      setTextStatus("disconnected");
    };
  }, [instanceName, host, project]);

  // Connect graphics console with robust DOM check for mobile
  const connectGraphics = useCallback(() => {
    if (!spiceReady || !isVM) return;
    
    // Check for screen element with retry for mobile devices
    const attemptConnect = (retryCount = 0) => {
      const screen = document.getElementById(screenId);
      if (!screen) {
        if (retryCount < 10) {
          // Retry with 100ms delay for mobile DOM readiness
          setTimeout(() => attemptConnect(retryCount + 1), 100);
        } else {
          console.error("[Console] Screen element not found after retries");
          setGraphicsStatus("error");
          setError("Graphics display not available");
        }
        return;
      }
      
      // Prevent double connection attempt
      if (graphicsConnectingRef.current) return;
      graphicsConnectingRef.current = true;

      if (spiceRef.current) {
        try { spiceRef.current.stop(); } catch (e) {}
        spiceRef.current = null;
      }

      // Reset stabilization flag
      isStabilizedRef.current = false;
      setGraphicsStatus("connecting");
      try {
        const wsUrl = getGraphicsWsUrl(instanceName, host, project);
        const SpiceMainConn = (window as any).SpiceMainConn;
        if (!SpiceMainConn) throw new Error("SpiceMainConn not available");

        const sc = new SpiceMainConn({
          uri: wsUrl,
          screen_id: screenId,
          password: "",
          onerror: (e: Error | string) => {
            const msg = typeof e === "string" ? e : e?.message || "Connection error";
            console.error("[Console] SPICE error:", msg);
            setGraphicsStatus("error");
            setError(msg);
            isStabilizedRef.current = false;
            graphicsConnectingRef.current = false; // Allow retry
          },
          onsuccess: () => {
            setGraphicsStatus("connected");
            graphicsConnectingRef.current = false; // Connection complete
            
            // Set stabilization flag after 500ms to prevent backtick spam on first input
            setTimeout(() => {
              isStabilizedRef.current = true;
            }, 500);
          },
          onagent: () => {
            // SPICE agent connected
          },
        });

        spiceRef.current = sc;
      } catch (error) {
        const msg = error instanceof Error ? error.message : "Failed to connect";
        console.error("[Console] Graphics connection error:", error);
        setGraphicsStatus("error");
        setError(msg);
        graphicsConnectingRef.current = false; // Allow retry
      }
    };
    
    // Start connection attempt with retry logic
    attemptConnect();
  }, [spiceReady, isVM, instanceName, host, project, screenId]);

  // Handle resize
  const handleResize = useCallback(() => {
    if (fitAddonRef.current && activeTab === "text") {
      try { fitAddonRef.current.fit(); } catch (e) {}
    }
  }, [activeTab]);

  // Search
  const handleSearch = useCallback(() => {
    if (searchAddonRef.current && searchTerm) {
      searchAddonRef.current.findNext(searchTerm);
    }
  }, [searchTerm]);

  // Reconnect
  const handleReconnect = useCallback(() => {
    if (activeTab === "text") {
      if (textWsRef.current) textWsRef.current.close();
      if (xtermRef.current) xtermRef.current.clear();
      connectText();
    } else {
      connectGraphics();
    }
  }, [activeTab, connectText, connectGraphics]);

  // important: guard the reconnect. It closes the socket and opens a new one, so
  // a rapid double-click tore the session down twice and raced the rebuild. The
  // guard also gives the icon-only button the pending state it never had.
  const { pending: reconnectPending, run: runReconnect } = useManualRefresh(
    handleReconnect,
    { errorTitle: "Reconnect failed" },
  );

  // Send Ctrl+Alt+Del
  const sendCtrlAltDel = useCallback(() => {
    if (spiceRef.current?.sendCtrlAltDel) {
      spiceRef.current.sendCtrlAltDel();
      toast({ title: "Sent Ctrl+Alt+Del" });
    }
  }, []);

  // BIOS key spam - send key 15 times via SPICE input channel
  const handleBiosSpam = useCallback(async (key: string, keyCode: number) => {
    if (biosSpamming) return;
    
    setBiosSpamming(true);
    toast({ title: `Spamming ${key}`, description: "Sending key 15 times..." });

    for (let i = 0; i < 15; i++) {
      const success = sendSpiceKey(keyCode);
      if (!success && i === 0) {
        toast({ title: "Failed", description: "SPICE connection not ready", variant: "destructive" });
        setBiosSpamming(false);
        return;
      }
      await new Promise(r => setTimeout(r, 100));
    }

    setBiosSpamming(false);
    toast({ title: "Done", description: `Sent ${key} 15 times` });
  }, [biosSpamming, sendSpiceKey]);

  // Handle virtual keyboard key press
  const handleVirtualKeyPress = useCallback((key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }) => {
    // Map key names to JavaScript keyCodes (used by SPICE scancode mapping)
    const keyCodeMap: Record<string, number> = {
      'Escape': 27, 'Esc': 27,
      'F1': 112, 'F2': 113, 'F3': 114, 'F4': 115,
      'F5': 116, 'F6': 117, 'F7': 118, 'F8': 119,
      'F9': 120, 'F10': 121, 'F11': 122, 'F12': 123,
      'Delete': 46, 'Del': 46,
      'Insert': 45,
      'Home': 36, 'End': 35,
      'PageUp': 33, 'PageDown': 34,
      'ArrowUp': 38, 'ArrowDown': 40, 'ArrowLeft': 37, 'ArrowRight': 39,
      'Enter': 13, 'Tab': 9, 'Backspace': 8, 'Space': 32,
      'Shift': 16, 'Control': 17, 'Ctrl': 17, 'Alt': 18,
      'CapsLock': 20, 'NumLock': 144, 'ScrollLock': 145,
    };
    
    // Handle special keys for terminal (xterm escape sequences)
    const specialKeyMap: Record<string, string> = {
      'Enter': '\r',
      'Tab': '\t',
      'Escape': '\x1b',
      'Backspace': '\x7f',
      'Delete': '\x1b[3~',
      'ArrowUp': '\x1b[A',
      'ArrowDown': '\x1b[B',
      'ArrowRight': '\x1b[C',
      'ArrowLeft': '\x1b[D',
      'Home': '\x1b[H',
      'End': '\x1b[F',
      'PageUp': '\x1b[5~',
      'PageDown': '\x1b[6~',
      'Insert': '\x1b[2~',
      'F1': '\x1bOP',
      'F2': '\x1bOQ',
      'F3': '\x1bOR',
      'F4': '\x1bOS',
      'F5': '\x1b[15~',
      'F6': '\x1b[17~',
      'F7': '\x1b[18~',
      'F8': '\x1b[19~',
      'F9': '\x1b[20~',
      'F10': '\x1b[21~',
      'F11': '\x1b[23~',
      'F12': '\x1b[24~',
    };
    
    // Send to console
    if (activeTab === 'text') {
      // Text console - send via WebSocket
      let processedKey = key;
      
      // For single character keys, apply shift for uppercase
      if (key.length === 1 && modifiers?.shift) {
        processedKey = key.toUpperCase();
      }
      
      // Check if it's a special key
      if (specialKeyMap[key]) {
        processedKey = specialKeyMap[key];
      }
      
      // Handle Ctrl+key combinations for terminal
      if (modifiers?.ctrl && key.length === 1) {
        const charCode = key.toLowerCase().charCodeAt(0);
        if (charCode >= 97 && charCode <= 122) {
          processedKey = String.fromCharCode(charCode - 96);
        }
      }
      
      if (textWsRef.current?.readyState === WebSocket.OPEN) {
        textWsRef.current.send(JSON.stringify({ type: "input", data: processedKey }));
      }
    } else if (activeTab === 'graphics') {
      // Graphics console - send via SPICE input channel
      
      // Handle Ctrl+Alt+Delete
      if (modifiers?.ctrl && modifiers?.alt && (key === 'Delete' || key === 'Del')) {
        sendCtrlAltDel();
        return;
      }
      
      // Handle modifier keys
      if (modifiers?.shift) sendSpiceKey(16, true, false); // Shift down
      if (modifiers?.ctrl) sendSpiceKey(17, true, false);  // Ctrl down
      if (modifiers?.alt) sendSpiceKey(18, true, false);   // Alt down
      
      // Send the key
      if (keyCodeMap[key]) {
        sendSpiceKey(keyCodeMap[key]);
      } else if (key.length === 1) {
        // For regular characters, get keyCode from uppercase char
        const keyCode = key.toUpperCase().charCodeAt(0);
        sendSpiceKey(keyCode);
      }
      
      // Release modifier keys
      if (modifiers?.alt) sendSpiceKey(18, false, true);   // Alt up
      if (modifiers?.ctrl) sendSpiceKey(17, false, true);  // Ctrl up
      if (modifiers?.shift) sendSpiceKey(16, false, true); // Shift up
      
      // Focus the SPICE canvas so user can continue typing with physical keyboard
      setTimeout(() => {
        const screen = document.getElementById(screenId);
        if (screen) {
          const canvas = screen.querySelector('canvas');
          if (canvas) (canvas as HTMLCanvasElement).focus();
        }
      }, 10);
    }
  }, [activeTab, sendSpiceKey, sendCtrlAltDel, screenId]);

  // Handle clipboard insert
  const handleClipboardInsert = useCallback(async (text: string) => {
    if (activeTab === 'text') {
      // Text console - send via WebSocket
      if (textWsRef.current?.readyState === WebSocket.OPEN) {
        textWsRef.current.send(JSON.stringify({ type: "input", data: text }));
        toast({ title: "Inserted", description: "Text sent to console" });
      }
    } else if (activeTab === 'graphics') {
      // Graphics console - send via SPICE input channel character by character
      for (let i = 0; i < text.length; i++) {
        const char = text[i];
        let keyCode = char.toUpperCase().charCodeAt(0);
        
        // Handle special characters
        if (char === ' ') keyCode = 32;
        else if (char === '\n' || char === '\r') keyCode = 13;
        else if (char === '\t') keyCode = 9;
        
        // Check if shift is needed for uppercase
        const needsShift = char !== char.toLowerCase() && char === char.toUpperCase() && /[A-Z]/.test(char);
        
        if (needsShift) {
          sendSpiceKey(16, true, false); // Shift down
        }
        
        sendSpiceKey(keyCode);
        
        if (needsShift) {
          sendSpiceKey(16, false, true); // Shift up
        }
        
        // Small delay between characters
        if (i < text.length - 1) {
          await new Promise(r => setTimeout(r, 20));
        }
      }
      toast({ title: "Inserted", description: "Text sent to console" });
      
      // Focus the SPICE canvas so user can continue interacting
      setTimeout(() => {
        const screen = document.getElementById(screenId);
        if (screen) {
          const canvas = screen.querySelector('canvas');
          if (canvas) (canvas as HTMLCanvasElement).focus();
        }
      }, 10);
    }
  }, [activeTab, sendSpiceKey, screenId]);

  // Zoom handlers - refit terminal when zoom changes
  const handleZoomIn = useCallback(() => {
    if (zoom < 200) {
      setZoom(z => {
        const newZoom = Math.min(z + 10, 200);
        // Refit terminal after zoom change
        if (activeTab === 'text' && fitAddonRef.current) {
          setTimeout(() => {
            try { fitAddonRef.current?.fit(); } catch (e) {}
          }, 50);
        }
        return newZoom;
      });
    }
  }, [zoom, activeTab]);
  
  const handleZoomOut = useCallback(() => {
    if (zoom > 50) {
      setZoom(z => {
        const newZoom = Math.max(z - 10, 50);
        // Refit terminal after zoom change
        if (activeTab === 'text' && fitAddonRef.current) {
          setTimeout(() => {
            try { fitAddonRef.current?.fit(); } catch (e) {}
          }, 50);
        }
        return newZoom;
      });
    }
  }, [zoom, activeTab]);
  
  const handleZoomReset = useCallback(() => {
    setZoom(100);
    // Refit terminal after zoom reset
    if (activeTab === 'text' && fitAddonRef.current) {
      setTimeout(() => {
        try { fitAddonRef.current?.fit(); } catch (e) {}
      }, 50);
    }
  }, [activeTab]);

  // Focus console helper - focuses the appropriate console element
  const focusConsole = useCallback(() => {
    if (activeTab === 'text') {
      xtermRef.current?.focus();
    } else if (activeTab === 'graphics') {
      const screen = document.getElementById(screenId);
      if (screen) {
        const canvas = screen.querySelector('canvas');
        if (canvas) {
          (canvas as HTMLCanvasElement).focus();
        } else {
          screen.focus();
        }
      }
    }
  }, [activeTab, screenId]);

  // Execute instance operation
  // `force` power-cycles instead of asking the guest to shut down: required for
  // a VM with no OS installed (or one at a boot menu/UEFI shell), which has
  // nothing listening for the ACPI request.
  const executeOperation = useCallback(async (operation: OperationType, force = false) => {
    setOperationLoading(operation);

    const transitionStatus: Record<OperationType, InstanceStatus> = {
      start: "Starting",
      stop: "Stopping",
      restart: "Restarting",
    };
    setInstanceStatus(transitionStatus[operation]);

    try {
      const response = await fetch(
        `${API_URL}/instances/${encodeURIComponent(instanceName)}/${operation}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
          },
          body: JSON.stringify({ host, force, timeout: 30 }),
        }
      );

      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: "Unknown error" }));
        throw new Error(error.detail || `Failed to ${operation} instance`);
      }

      const result = await response.json();
      const statusMap: Record<string, InstanceStatus> = {
        running: "Running",
        stopped: "Stopped",
        Running: "Running",
        Stopped: "Stopped",
      };
      const newStatus = statusMap[result.new_status] || "Unknown";
      setInstanceStatus(newStatus);

      toast({
        title: `Instance ${operation}ed successfully`,
        description: `${instanceName} is now ${newStatus.toLowerCase()}`,
      });

      if (operation === "restart") {
        setTimeout(() => handleReconnect(), 2000);
      }
    } catch (error) {
      setInstanceStatus("Error");
      toast({
        title: `Failed to ${operation} instance`,
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setOperationLoading(null);
    }
  }, [instanceName, host, handleReconnect]);

  // Request operation with confirmation
  const requestOperation = useCallback((operation: OperationType) => {
    if (operation === "start") {
      executeOperation(operation);
      return;
    }

    const dialogConfig: Record<OperationType, { title: string; description: string }> = {
      start: { title: "", description: "" },
      stop: {
        title: `Stop ${instanceName}?`,
        description: `This will gracefully stop the instance. The console connection will be lost.`,
      },
      restart: {
        title: `Restart ${instanceName}?`,
        description: `This will restart the instance. The console will attempt to reconnect after restart.`,
      },
    };

    setConfirmDialog({
      open: true,
      operation,
      ...dialogConfig[operation],
      force: false,
    });
  }, [instanceName, executeOperation]);

  const handleConfirm = useCallback(() => {
    if (confirmDialog.operation) {
      executeOperation(confirmDialog.operation, confirmDialog.force);
    }
    setConfirmDialog((prev) => ({ ...prev, open: false }));
  }, [confirmDialog.operation, confirmDialog.force, executeOperation]);

  const isRunning = instanceStatus === "Running";
  const isStopped = instanceStatus === "Stopped";
  const isTransitioning = ["Starting", "Stopping", "Restarting"].includes(instanceStatus);
  const showStart = isStopped || instanceStatus === "Unknown" || instanceStatus === "Error";
  const showRestartStop = isRunning || instanceStatus === "Unknown" || instanceStatus === "Error";
  const isOperationDisabled = isTransitioning || operationLoading !== null;

  const currentStatus = activeTab === "text" ? textStatus : graphicsStatus;

  // Initialize text console when tab is active and session check complete
  useEffect(() => {
    if (activeTab === "text" && sessionCheckComplete && !sessionConflict.show) {
      // Small delay to ensure DOM is ready after visibility change
      const timer = setTimeout(() => {
        initTextTerminal();
        connectText();
        // Refit terminal when tab becomes active
        if (fitAddonRef.current) {
          try { fitAddonRef.current.fit(); } catch (e) {}
        }
      }, 50);
      return () => clearTimeout(timer);
    }
    return () => {
      // Cleanup handled elsewhere
    };
  }, [activeTab, sessionCheckComplete, sessionConflict.show, initTextTerminal, connectText]);

  // Initialize graphics console when tab is active and session check complete
  useEffect(() => {
    if (activeTab === "graphics" && spiceReady && sessionCheckComplete && !sessionConflict.show) {
      // Small delay to ensure DOM is ready
      const timer = setTimeout(() => connectGraphics(), 100);
      return () => clearTimeout(timer);
    }
  }, [activeTab, spiceReady, sessionCheckComplete, sessionConflict.show, connectGraphics]);

  // Auto-focus canvas when graphics console becomes connected or when switching to graphics tab
  useEffect(() => {
    if (activeTab === "graphics" && graphicsStatus === "connected") {
      // Focus the canvas after a small delay to ensure it's rendered
      const timer = setTimeout(() => {
        const screen = document.getElementById(screenId);
        if (screen) {
          const canvas = screen.querySelector('canvas');
          if (canvas) {
            (canvas as HTMLCanvasElement).focus();
          }
        }
      }, 200);
      return () => clearTimeout(timer);
    }
  }, [activeTab, graphicsStatus, screenId]);

  // Track canvas size changes for proper scaling container sizing
  useEffect(() => {
    if (graphicsStatus !== 'connected') return;
    
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
  }, [graphicsStatus, screenId]);

  // Apply zoom by scaling canvas CSS dimensions (preserves mouse coordinate accuracy)
  useEffect(() => {
    if (graphicsStatus !== 'connected' || canvasSize.width === 0) return;
    
    const screen = document.getElementById(screenId);
    if (!screen) return;
    
    const canvas = screen.querySelector('canvas') as HTMLCanvasElement;
    if (!canvas) return;
    
    // Set CSS dimensions to scaled size - browser handles mouse coord transformation
    canvas.style.width = `${canvasSize.width * scale}px`;
    canvas.style.height = `${canvasSize.height * scale}px`;
  }, [graphicsStatus, screenId, scale, canvasSize]);

  // Touch event support for mobile - SPICE only listens for mouse events
  // We need to translate touch events to mouse events for mobile drag to work inside the console
  useEffect(() => {
    if (graphicsStatus !== 'connected') return;
    
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
  }, [graphicsStatus, screenId]);

  // Block keyboard events on canvas during stabilization period
  // This intercepts events BEFORE SPICE's native handlers to prevent backtick spam
  useEffect(() => {
    if (graphicsStatus !== 'connected') return;
    
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
  }, [graphicsStatus, screenId]);

  // Window-level keyboard listener - captures keys even without focus
  // This allows keyboard interaction without needing to click on the console first
  useEffect(() => {
    if (activeTab !== 'graphics' || graphicsStatus !== 'connected') return;
    
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
      const sc = spiceRef.current as any;
      if (!sc || !sc.inputs || sc.inputs.state !== 'ready') return;
      
      // Skip if user is typing in an input field
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
        return;
      }
      
      // CRITICAL: Check if the SPICE screen or its canvas is the event target
      // If so, SPICE's native handlers (attached in display.js) will process it
      // We must NOT double-send keys, especially in BIOS which is sensitive to this
      const screen = document.getElementById(screenId);
      if (screen && (target === screen || screen.contains(target))) {
        // Canvas/screen is focused, SPICE handles it natively - don't double-send
        // Only prevent backtick spam in this case
        if (e.repeat && (e.key === '`' || e.key === '~')) {
          e.preventDefault();
        }
        return;
      }
      
      // Prevent backtick key repeat spam - ignore if key is being held
      if (e.repeat && (e.key === '`' || e.key === '~')) {
        e.preventDefault();
        return;
      }
      
      // Let SPICE's native handler process the key via the inputs channel
      // This is for when focus is elsewhere (ex; on a button) but user wants to type
      const win = window as any;
      if (win.handle_keydown) {
        win.handle_keydown.call({ sc }, e);
      }
    };
    
    const handleWindowKeyUp = (e: KeyboardEvent) => {
      // Don't process until connection is stabilized
      if (!stabilized) return;
      
      const sc = spiceRef.current as any;
      if (!sc || !sc.inputs || sc.inputs.state !== 'ready') return;
      
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
        return;
      }
      
      // CRITICAL: Same check for keyUp - don't double-send if canvas is focused
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
  }, [activeTab, graphicsStatus]);

  // Handle window resize and cleanup on unload
  useEffect(() => {
    window.addEventListener("resize", handleResize);
    const heartbeat = setInterval(() => {
      if (textWsRef.current?.readyState === WebSocket.OPEN) {
        textWsRef.current.send(JSON.stringify({ type: "ping" }));
      }
    }, 30000);
    
    // Kill sessions when tab is closed
    const handleBeforeUnload = () => {
      // Use sendBeacon with Blob for POST request during unload
      const killUrl = `${API_URL}/console/${encodeURIComponent(instanceName)}/kill-all-sessions?host=${encodeURIComponent(host)}`;
      // sendBeacon needs a body (Blob) to trigger POST method
      const blob = new Blob([''], { type: 'application/json' });
      navigator.sendBeacon(killUrl, blob);
      
      // Also close connections
      if (textWsRef.current) textWsRef.current.close();
      if (spiceRef.current) {
        try { spiceRef.current.stop(); } catch (e) {}
      }
    };
    
    window.addEventListener("beforeunload", handleBeforeUnload);

    return () => {
      window.removeEventListener("resize", handleResize);
      window.removeEventListener("beforeunload", handleBeforeUnload);
      clearInterval(heartbeat);
      if (textWsRef.current) textWsRef.current.close();
      if (xtermRef.current) xtermRef.current.dispose();
      if (spiceRef.current) {
        try { spiceRef.current.stop(); } catch (e) {}
      }
    };
  }, [handleResize, instanceName, host]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key === "F") {
        e.preventDefault();
        setShowSearch((s) => !s);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  // Status badge
  const StatusBadge = () => {
    switch (currentStatus) {
      case "connecting":
      case "loading":
        return (
          <Badge variant="secondary" className="gap-1 bg-yellow-500/20 text-yellow-400">
            <Loader2 className="h-3 w-3 animate-spin" />
            Connecting
          </Badge>
        );
      case "connected":
        return (
          <Badge variant="secondary" className="gap-1 bg-green-500/20 text-green-400">
            <CheckCircle2 className="h-3 w-3" />
            Connected
          </Badge>
        );
      case "error":
        return (
          <Badge variant="secondary" className="gap-1 bg-red-500/20 text-red-400">
            <AlertCircle className="h-3 w-3" />
            Error
          </Badge>
        );
      default:
        return (
          <Badge variant="secondary" className="gap-1 bg-gray-500/20 text-gray-400">
            Disconnected
          </Badge>
        );
    }
  };

  return (
    <div className="flex flex-col h-screen bg-terminal">
      {/* Header */}
      <div className="flex items-center justify-between h-12 px-4 bg-terminal-deep border-b border-terminal-border">
        {/* Left */}
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 text-gray-400 hover:text-white"
            aria-label="Close this console window"
            title="Close this console window"
            onClick={() => window.close()}
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </Button>
          {activeTab === "graphics" ? (
            <Monitor className="h-5 w-5 text-purple-400" />
          ) : (
            <TerminalIcon className="h-5 w-5 text-blue-400" />
          )}
          <div className="flex items-center gap-2">
            <span className="font-medium text-gray-200">{instanceName}</span>
            <span className="text-gray-500">@</span>
            <span className="text-gray-400">{host}</span>
            <span className="text-gray-600 text-sm">({project})</span>
          </div>
          <StatusBadge />
          {/* A human watching this console could not tell that an agent was driving the same X
              session. The Sessions page shows it afterwards; this shows it while it matters. */}
          <AgentActivityBadge instanceName={instanceName} accessToken={getAccessToken()} />
        </div>

        {/* Center - Tabs for VMs */}
        {isVM && (
          <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as ConsoleType)}>
            <TabsList className="h-8 bg-terminal-raised">
              <TabsTrigger value="graphics" className="h-6 px-3 text-xs gap-1.5">
                <Monitor className="h-3.5 w-3.5" />
                Graphics
              </TabsTrigger>
              <TabsTrigger value="text" className="h-6 px-3 text-xs gap-1.5">
                <TerminalIcon className="h-3.5 w-3.5" />
                Text
              </TabsTrigger>
            </TabsList>
          </Tabs>
        )}

        {/* Right */}
        <div className="flex items-center gap-2">
          <TooltipProvider delayDuration={100}>
            {/* Instance Operations */}
            <div className="flex items-center gap-1 mr-2 border-r border-gray-600 pr-2">
              {showStart && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className={cn(
                        "h-8 w-8 text-gray-400 hover:text-green-400 hover:bg-green-400/10",
                        operationLoading === "start" && "text-green-400"
                      )}
                      onClick={() => requestOperation("start")}
                      disabled={isOperationDisabled}
                    >
                      {operationLoading === "start" ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Play className="h-4 w-4" />
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Start Instance</TooltipContent>
                </Tooltip>
              )}

              {showRestartStop && (
                <>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className={cn(
                          "h-8 w-8 text-gray-400 hover:text-blue-400 hover:bg-blue-400/10",
                          operationLoading === "restart" && "text-blue-400"
                        )}
                        onClick={() => requestOperation("restart")}
                        disabled={isOperationDisabled}
                      >
                        {operationLoading === "restart" ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <RotateCcw className="h-4 w-4" />
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Restart Instance</TooltipContent>
                  </Tooltip>

                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className={cn(
                          "h-8 w-8 text-gray-400 hover:text-red-400 hover:bg-red-400/10",
                          operationLoading === "stop" && "text-red-400"
                        )}
                        onClick={() => requestOperation("stop")}
                        disabled={isOperationDisabled}
                      >
                        {operationLoading === "stop" ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Square className="h-4 w-4" />
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Stop Instance</TooltipContent>
                  </Tooltip>
                </>
              )}
            </div>

            {/* Clipboard Manager */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    "h-8 w-8 text-gray-400 hover:text-white",
                    showClipboard && "bg-white/10 text-white"
                  )}
                  onClick={() => setShowClipboard(s => !s)}
                >
                  <ClipboardList className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Clipboard Manager</TooltipContent>
            </Tooltip>

            {/* On-Screen Keyboard */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    "h-8 w-8 text-gray-400 hover:text-white",
                    showKeyboard && "bg-white/10 text-white"
                  )}
                  onClick={() => setShowKeyboard(s => !s)}
                >
                  <Keyboard className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>On-Screen Keyboard</TooltipContent>
            </Tooltip>

            {/* On-Screen Cursor */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    "h-8 w-8 text-gray-400 hover:text-white",
                    showCursor && "bg-white/10 text-white"
                  )}
                  onClick={() => setShowCursor(s => !s)}
                >
                  <MousePointer2 className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>On-Screen Cursor</TooltipContent>
            </Tooltip>

            {/* Zoom Controls */}
            <div className="flex items-center gap-0.5 border-l border-gray-600 pl-2 ml-1">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-gray-400 hover:text-white"
                    onClick={handleZoomOut}
                    disabled={zoom <= 50}
                  >
                    <ZoomOut className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Zoom Out</TooltipContent>
              </Tooltip>
              
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-8 px-2 text-gray-400 hover:text-white text-xs font-mono"
                    onClick={handleZoomReset}
                  >
                    {zoom}%
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Reset Zoom (100%)</TooltipContent>
              </Tooltip>
              
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-gray-400 hover:text-white"
                    onClick={handleZoomIn}
                    disabled={zoom >= 200}
                  >
                    <ZoomIn className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Zoom In</TooltipContent>
              </Tooltip>
            </div>

            {/* Divider after zoom */}
            <div className="h-5 w-px bg-gray-600 mx-1" />

            {/* Search (text only) */}
            {activeTab === "text" && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className={cn(
                      "h-8 w-8 text-gray-400 hover:text-white",
                      showSearch && "bg-white/10 text-white"
                    )}
                    onClick={() => setShowSearch((s) => !s)}
                  >
                    <Search className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Search (Ctrl+Shift+F)</TooltipContent>
              </Tooltip>
            )}

            {/* BIOS Boot Keys (for VMs) */}
            {isVM && activeTab === "graphics" && (
              <DropdownMenu>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-8 px-2 text-gray-400 hover:text-white gap-1"
                        disabled={biosSpamming}
                      >
                        {biosSpamming ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Rocket className="h-4 w-4" />
                        )}
                        <span className="text-xs hidden sm:inline">BIOS</span>
                        <ChevronDown className="h-3 w-3" />
                      </Button>
                    </DropdownMenuTrigger>
                  </TooltipTrigger>
                  <TooltipContent>BIOS Boot Keys</TooltipContent>
                </Tooltip>
                <DropdownMenuContent align="end" className="w-56 bg-terminal-raised border-terminal-border">
                  <DropdownMenuLabel className="text-xs text-gray-400">
                    Spam key to enter BIOS/Boot Menu
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator className="bg-terminal-border" />
                  {BIOS_KEYS.map((bios) => (
                    <DropdownMenuItem 
                      key={bios.key}
                      onClick={() => handleBiosSpam(bios.key, bios.keyCode)}
                      disabled={biosSpamming}
                      className="text-gray-200 focus:bg-terminal-border focus:text-white"
                    >
                      <div className="flex items-center justify-between w-full">
                        <span className="font-mono font-medium">{bios.label}</span>
                        <span className="text-xs text-gray-500">{bios.description}</span>
                      </div>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-busy={reconnectPending}
                  aria-label="Reconnect"
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-gray-400 hover:text-white"
                  disabled={reconnectPending}
                  onClick={() => void runReconnect()}
                >
                  <RefreshCw
                    className={cn(
                      "h-4 w-4",
                      reconnectPending && "animate-spin motion-reduce:animate-none",
                    )}
                  />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reconnect</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      {/* Confirmation Dialog */}
      <AlertDialog
        open={confirmDialog.open}
        onOpenChange={(open) => setConfirmDialog((prev) => ({ ...prev, open }))}
      >
        <AlertDialogContent className="bg-terminal border-terminal-border">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-gray-200">
              {confirmDialog.title}
            </AlertDialogTitle>
            <AlertDialogDescription className="text-gray-400">
              {confirmDialog.description}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {/* A graceful stop asks the guest OS to shut down over ACPI. A VM with
              no OS installed, or one sitting at a boot menu/UEFI shell, has
              nothing listening and will just time out. Force is the only way to
              power-cycle those. Copy lives in lib/instance-action-copy.ts, which
              two other dialogs share. */}
          <label className="flex items-start gap-2 rounded-md border border-terminal-border p-3 cursor-pointer">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirmDialog.force}
              onChange={(e) =>
                setConfirmDialog((prev) => ({ ...prev, force: e.target.checked }))
              }
            />
            <span className="text-sm">
              <span className="font-medium text-gray-200">{FORCE_OPTION_LABEL}</span>
              <span className="block text-xs text-gray-400 mt-0.5">
                {confirmDialog.force ? FORCE_ON_HINT : FORCE_OFF_HINT}
              </span>
            </span>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel className="bg-transparent border-gray-600 text-gray-300 hover:bg-gray-800">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirm}
              className={cn(
                confirmDialog.operation === "stop" && "bg-red-600 hover:bg-red-700",
                confirmDialog.operation === "restart" && "bg-blue-600 hover:bg-blue-700"
              )}
            >
              {confirmDialog.force ? "Force " : ""}
              {confirmDialog.operation === "stop" ? "Stop" : "Restart"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Session Conflict Dialog */}
      <AlertDialog open={sessionConflict.show}>
        <AlertDialogContent className="bg-terminal border-terminal-border max-w-lg">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-yellow-400">
              Active Console Session Detected
            </AlertDialogTitle>
            <AlertDialogDescription className="text-gray-300">
              Another console session is already running for <strong>{instanceName}</strong>.
              <br />
              Running multiple console sessions may cause display issues. You can:
              <ul className="list-disc list-inside mt-2 text-sm">
                <li>Kill the existing session and continue</li>
                <li>Continue anyway (not recommended)</li>
                <li>Cancel and use the existing session</li>
              </ul>
            </AlertDialogDescription>
          </AlertDialogHeader>
          
          {/* Session list */}
          <div className="max-h-40 overflow-y-auto space-y-2 my-2">
            {sessionConflict.sessions.map((session) => (
              <div
                key={session.operation_id}
                className="flex items-center justify-between p-2 bg-gray-800/50 rounded text-sm"
              >
                <div className="flex-1 min-w-0">
                  <div className="break-all text-gray-200">{session.operation_id}</div>
                  <div className="text-gray-500 text-xs">{session.description}</div>
                </div>
                <div className="text-gray-400 text-xs ml-2">
                  {new Date(session.created_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </div>
              </div>
            ))}
          </div>
          
          <AlertDialogFooter className="flex gap-2">
            <AlertDialogCancel
              className="bg-transparent border-gray-600 text-gray-300 hover:bg-gray-800"
              onClick={() => window.close()}
            >
              Cancel
            </AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={async () => {
                // Kill all sessions
                for (const session of sessionConflict.sessions) {
                  await handleKillSession(session.operation_id);
                }
                setSessionConflict({ show: false, sessions: [] });
              }}
              disabled={killingSession}
            >
              {killingSession && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Kill & Continue
            </Button>
            <Button
              variant="outline"
              className="border-yellow-500/50 text-yellow-400 hover:bg-yellow-500/10"
              onClick={() => setSessionConflict({ show: false, sessions: [] })}
            >
              Continue Anyway
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Search bar (text mode only) */}
      {showSearch && activeTab === "text" && (
        <div className="flex items-center gap-2 px-4 py-2 bg-terminal-raised border-b border-terminal-border">
          <Search className="h-4 w-4 text-gray-400" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="Search console output..."
            className="flex-1 bg-transparent text-sm text-gray-200 outline-none placeholder:text-gray-500"
            autoFocus
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs text-gray-400 hover:text-white"
            onClick={handleSearch}
          >
            Find Next
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-gray-400 hover:text-white"
            onClick={() => setShowSearch(false)}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}

      {/* Console Content */}
      <div className="flex-1 overflow-hidden relative flex flex-col">
        {/* Main area with optional sidebar */}
        <div className="flex-1 overflow-hidden relative flex">
          {/* Clipboard Manager Sidebar */}
          {showClipboard && (
            <ClipboardManager
              onClose={() => setShowClipboard(false)}
              onInsert={handleClipboardInsert}
            />
          )}
          
          {/* Console display wrapper */}
          <div className="flex-1 overflow-hidden relative">
            {/* Session check loading overlay */}
            {!sessionCheckComplete && (
              <div className="absolute inset-0 flex flex-col items-center justify-center bg-terminal z-20">
                <Loader2 className="h-8 w-8 animate-spin text-blue-400 mb-4" />
                <p className="text-gray-400">Checking for active sessions...</p>
              </div>
            )}

            {/* Console display area */}
            <div 
              className="absolute inset-0 overflow-hidden"
              style={activeTab === "text" && zoom !== 100 ? { zoom: `${zoom}%` } : undefined}
            >
              {/* Text Console - use visibility instead of hidden to keep ref alive */}
              <div
                ref={terminalRef}
                className={cn(
                  "absolute inset-0 p-2",
                  activeTab !== "text" && "invisible pointer-events-none"
                )}
                style={{ 
                  zIndex: activeTab === "text" ? 10 : 1,
                  opacity: activeTab === "text" ? 1 : 0,
                }}
                onClick={() => xtermRef.current?.focus()}
              />

              {/* Graphics Console */}
              {isVM && (
                <div
                  ref={graphicsRef}
                  className={cn(
                    "absolute inset-0 bg-black",
                    activeTab !== "graphics" && "invisible pointer-events-none"
                  )}
                  style={{
                    overflow: 'auto', // Allow scrolling if VM resolution > viewport
                    display: 'flex',
                    alignItems: 'flex-start', // Align to top
                    justifyContent: 'flex-start', // Align to left - canvas needs to start at (0,0)
                    zIndex: activeTab === "graphics" ? 10 : 1,
                    opacity: activeTab === "graphics" ? 1 : 0,
                  }}
                  onClick={() => {
                    // Auto-focus the canvas when clicking anywhere in the graphics console area
                    const screen = document.getElementById(screenId);
                    if (screen) {
                      const canvas = screen.querySelector('canvas');
                      if (canvas) (canvas as HTMLCanvasElement).focus();
                    }
                  }}
                >
                  {/* SPICE screen container - canvas is rendered inside by spice-html5 */}
                  <div
                    id={screenId}
                    className="spice-screen"
                    style={{ 
                      cursor: "default",
                      position: "relative",
                    }}
                    tabIndex={0}
                    onMouseDown={(e) => e.stopPropagation()}
                    onClick={(e) => {
                      e.stopPropagation();
                      const canvas = e.currentTarget.querySelector('canvas');
                      if (canvas) (canvas as HTMLCanvasElement).focus();
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
              )}
            </div>

            {/* On-Screen Cursor Overlay - outside scroll container */}
            {showCursor && activeTab === "graphics" && (
              <OnScreenCursor
                screenId={screenId}
                isActive={showCursor}
                onClose={() => setShowCursor(false)}
              />
            )}
          </div>
        </div>

        {/* On-Screen Keyboard (at bottom, outside the flex row) */}
        {showKeyboard && (
          <OnScreenKeyboard
            onKeyPress={handleVirtualKeyPress}
            onClose={() => setShowKeyboard(false)}
          />
        )}

        {/* Session Intelligence & Resource Display Bar */}
        <ConsoleResourceBar
          sessionId={sessionId || null}
          instanceName={instanceName}
          host={host}
          project={project}
          connectionStatus={currentStatus}
          protocol={activeTab === "graphics" ? "graphics" : "terminal"}
        />
      </div>
    </div>
  );
}

// Export with Suspense wrapper
export default function ExpandedConsolePage() {
  return (
    <Suspense fallback={<ConsoleLoading />}>
      <ConsoleContent />
    </Suspense>
  );
}
