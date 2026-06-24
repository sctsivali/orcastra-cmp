/**
 * On-Screen Cursor - virtual cursor for mobile/touch (and desktop) consoles.
 * Lets users position a pointer and trigger left/right/double clicks, scroll, and
 * window drags inside the VM without a physical mouse.
 *
 * Mouse-mode aware (see lib/spice/osc-pointer):
 * - CLIENT mode (guest runs spice-vdagent): absolute positioning - a tap clicks
 *   exactly under the crosshair.
 * - SERVER mode (no agent): the guest only takes relative motion, so the cursor
 *   acts as a trackpad - dragging moves the guest pointer, a tap clicks where it is.
 *
 * Implementation notes:
 * - All state read inside event callbacks is mirrored in refs (no stale closures).
 * - SPICE event dispatch lives in lib/spice/osc-pointer (pure DOM helpers).
 */

"use client";

import * as React from "react";
import { useState, useRef, useCallback, useEffect } from "react";
import { cn } from "@/lib/utils";
import { MousePointer2, MousePointerClick, X, Move, Lock, Unlock, ChevronUp, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  getCanvas,
  getMouseMode,
  isReady,
  primeServerPosition,
  sendMove,
  sendDown,
  sendUp,
  sendClick as sendClickToCanvas,
  sendDoubleClick,
  sendScroll,
  type MouseMode,
} from "@/lib/spice/osc-pointer";

type ClickMode = "left" | "right";

interface Position {
  x: number;
  y: number;
}

interface OnScreenCursorProps {
  /** ID of the SPICE screen element to target */
  screenId: string;
  /** Whether the cursor is visible/active */
  isActive: boolean;
  /** Callback when cursor is closed */
  onClose?: () => void;
  /** Optional class name */
  className?: string;
}

// Cursor tip offset from the cursor element's top-left corner (matches the crosshair).
const CURSOR_TIP_OFFSET_X = 20;
const CURSOR_TIP_OFFSET_Y = 8;

export function OnScreenCursor({
  screenId,
  isActive,
  onClose,
  className,
}: OnScreenCursorProps) {
  // === STATE (for rendering) ===
  const [clickMode, setClickMode] = useState<ClickMode>("left");
  const [position, setPosition] = useState<Position>({ x: 150, y: 150 });
  const [isDragging, setIsDragging] = useState(false);
  const [showClickFeedback, setShowClickFeedback] = useState(false);
  const [isHoldMode, setIsHoldMode] = useState(false);
  const [isHolding, setIsHolding] = useState(false);
  const [mode, setMode] = useState<MouseMode>("unknown");

  // === REFS (for callbacks to avoid stale closures) ===
  const containerRef = useRef<HTMLDivElement>(null);
  const cursorRef = useRef<HTMLDivElement>(null);

  const positionRef = useRef<Position>({ x: 150, y: 150 });
  const isDraggingRef = useRef(false);
  const isHoldModeRef = useRef(false);
  const isHoldingRef = useRef(false);
  const clickModeRef = useRef<ClickMode>("left");
  const modeRef = useRef<MouseMode>("unknown");

  // Offset-based drag tracking (cursorPos = mousePos - offset).
  const dragOffsetRef = useRef<Position>({ x: 0, y: 0 });
  const dragStartTimeRef = useRef(0);
  const dragStartClientRef = useRef<Position>({ x: 0, y: 0 });

  // === SYNC STATE TO REFS ===
  useEffect(() => { positionRef.current = position; }, [position]);
  useEffect(() => { clickModeRef.current = clickMode; }, [clickMode]);
  useEffect(() => { isHoldModeRef.current = isHoldMode; }, [isHoldMode]);
  useEffect(() => { isHoldingRef.current = isHolding; }, [isHolding]);
  useEffect(() => { modeRef.current = mode; }, [mode]);

  // === CANVAS / MODE ===
  const canvas = useCallback(() => getCanvas(screenId), [screenId]);

  // Poll the negotiated mouse mode so the hint/crosshair reflect CLIENT vs SERVER.
  useEffect(() => {
    if (!isActive) return;
    const tick = () => setMode(getMouseMode(getCanvas(screenId)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [isActive, screenId]);

  /** Crosshair tip in viewport coordinates (where a client-mode click lands). */
  const getTipScreenPosition = useCallback((cursorPos: Position): Position | null => {
    const container = containerRef.current;
    if (!container) return null;
    const r = container.getBoundingClientRect();
    return { x: r.left + cursorPos.x + CURSOR_TIP_OFFSET_X, y: r.top + cursorPos.y + CURSOR_TIP_OFFSET_Y };
  }, []);

  // === ACTIONS ===
  const doClick = useCallback(() => {
    const tip = getTipScreenPosition(positionRef.current);
    const c = canvas();
    if (!tip || !isReady(c)) return;
    setShowClickFeedback(true);
    setTimeout(() => setShowClickFeedback(false), 150);
    void sendClickToCanvas(c, tip.x, tip.y, clickModeRef.current, modeRef.current);
  }, [canvas, getTipScreenPosition]);

  const doDoubleClick = useCallback(() => {
    const tip = getTipScreenPosition(positionRef.current);
    const c = canvas();
    if (!tip || !isReady(c)) return;
    setShowClickFeedback(true);
    setTimeout(() => setShowClickFeedback(false), 150);
    void sendDoubleClick(c, tip.x, tip.y, modeRef.current);
  }, [canvas, getTipScreenPosition]);

  const doScroll = useCallback((direction: "up" | "down") => {
    const tip = getTipScreenPosition(positionRef.current);
    const c = canvas();
    if (!tip || !isReady(c)) return;
    sendScroll(c, tip.x, tip.y, direction);
  }, [canvas, getTipScreenPosition]);

  const releaseHeldButton = useCallback(() => {
    const tip = getTipScreenPosition(positionRef.current);
    if (tip) sendUp(canvas(), tip.x, tip.y, clickModeRef.current);
    setIsHolding(false);
    isHoldingRef.current = false;
    setIsHoldMode(false);
    isHoldModeRef.current = false;
  }, [canvas, getTipScreenPosition]);

  const toggleHoldMode = useCallback(() => {
    const tip = getTipScreenPosition(positionRef.current);
    if (isHoldModeRef.current) {
      if (isHoldingRef.current) releaseHeldButton();
      else { setIsHoldMode(false); isHoldModeRef.current = false; }
      return;
    }
    if (!tip) return;
    const c = canvas();
    if (modeRef.current === "server" && tip) primeServerPosition(c, tip.x, tip.y);
    const ok = sendDown(c, tip.x, tip.y, clickModeRef.current, modeRef.current);
    if (ok) {
      setIsHolding(true); isHoldingRef.current = true;
      setIsHoldMode(true); isHoldModeRef.current = true;
    }
  }, [canvas, getTipScreenPosition, releaseHeldButton]);

  // === CURSOR POSITION UPDATE ===
  const updateCursorPosition = useCallback((clientX: number, clientY: number) => {
    const container = containerRef.current;
    if (!container || !isDraggingRef.current) return;

    const r = container.getBoundingClientRect();
    const newX = clientX - r.left - dragOffsetRef.current.x;
    const newY = clientY - r.top - dragOffsetRef.current.y;

    const cursorSize = 48;
    const clampedX = Math.max(0, Math.min(r.width - cursorSize, newX));
    const clampedY = Math.max(0, Math.min(r.height - cursorSize, newY));
    const newPos = { x: clampedX, y: clampedY };
    positionRef.current = newPos;
    setPosition(newPos);

    const tipX = r.left + clampedX + CURSOR_TIP_OFFSET_X;
    const tipY = r.top + clampedY + CURSOR_TIP_OFFSET_Y;
    const buttons = isHoldingRef.current ? (clickModeRef.current === "left" ? 1 : 2) : 0;

    // SERVER mode: dragging the puck always moves the guest pointer (relative
    // trackpad). CLIENT mode: only send while holding (window drag); a plain drag
    // just repositions the absolute click target.
    if (modeRef.current === "server") {
      sendMove(canvas(), tipX, tipY, buttons);
    } else if (isHoldingRef.current) {
      sendMove(canvas(), tipX, tipY, buttons);
    }
  }, [canvas]);

  // === DRAG START / END ===
  const startDrag = useCallback((clientX: number, clientY: number) => {
    const container = containerRef.current;
    if (!container) return;
    const r = container.getBoundingClientRect();
    const currentPos = positionRef.current;
    dragOffsetRef.current = { x: clientX - r.left - currentPos.x, y: clientY - r.top - currentPos.y };
    dragStartClientRef.current = { x: clientX, y: clientY };
    dragStartTimeRef.current = Date.now();

    // Prime SERVER position so the first relative move doesn't jump.
    if (modeRef.current === "server") {
      const tip = getTipScreenPosition(currentPos);
      if (tip) primeServerPosition(canvas(), tip.x, tip.y);
    }

    isDraggingRef.current = true;
    setIsDragging(true);
  }, [canvas, getTipScreenPosition]);

  const endDrag = useCallback((clientX: number, clientY: number) => {
    if (!isDraggingRef.current) return;
    const duration = Date.now() - dragStartTimeRef.current;
    const dx = Math.abs(clientX - dragStartClientRef.current.x);
    const dy = Math.abs(clientY - dragStartClientRef.current.y);
    const movement = Math.sqrt(dx * dx + dy * dy);

    isDraggingRef.current = false;
    setIsDragging(false);

    // Tap: short, little movement, not holding -> single click.
    if (duration < 250 && movement < 15 && !isHoldModeRef.current) doClick();
  }, [doClick]);

  // === GLOBAL EVENT LISTENERS ===
  useEffect(() => {
    if (!isActive) return;

    const onMouseMove = (e: MouseEvent) => { if (!isDraggingRef.current) return; e.preventDefault(); updateCursorPosition(e.clientX, e.clientY); };
    const onMouseUp = (e: MouseEvent) => { if (!isDraggingRef.current) return; endDrag(e.clientX, e.clientY); };
    const onTouchMove = (e: TouchEvent) => {
      if (!isDraggingRef.current || e.touches.length !== 1) return;
      e.preventDefault();
      updateCursorPosition(e.touches[0].clientX, e.touches[0].clientY);
    };
    const onTouchEnd = (e: TouchEvent) => {
      if (!isDraggingRef.current || e.changedTouches.length < 1) return;
      endDrag(e.changedTouches[0].clientX, e.changedTouches[0].clientY);
    };
    const onTouchCancel = () => { if (!isDraggingRef.current) return; isDraggingRef.current = false; setIsDragging(false); };

    document.addEventListener("mousemove", onMouseMove, { passive: false });
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("touchmove", onTouchMove, { passive: false });
    document.addEventListener("touchend", onTouchEnd);
    document.addEventListener("touchcancel", onTouchCancel);
    return () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.removeEventListener("touchmove", onTouchMove);
      document.removeEventListener("touchend", onTouchEnd);
      document.removeEventListener("touchcancel", onTouchCancel);
    };
  }, [isActive, updateCursorPosition, endDrag]);

  // === ELEMENT EVENT HANDLERS ===
  const handleMouseDown = useCallback((e: React.MouseEvent) => { e.preventDefault(); e.stopPropagation(); startDrag(e.clientX, e.clientY); }, [startDrag]);
  const handleTouchStart = useCallback((e: React.TouchEvent) => { e.stopPropagation(); if (e.touches.length === 1) startDrag(e.touches[0].clientX, e.touches[0].clientY); }, [startDrag]);

  // === LIFECYCLE EFFECTS ===
  // Center cursor when activated.
  useEffect(() => {
    if (isActive && containerRef.current) {
      const r = containerRef.current.getBoundingClientRect();
      const centerPos = { x: r.width / 2 - 24, y: r.height / 2 - 24 };
      positionRef.current = centerPos;
      setPosition(centerPos);
    }
  }, [isActive]);

  // Release the held button when deactivated.
  useEffect(() => {
    if (!isActive && isHoldingRef.current) releaseHeldButton();
  }, [isActive, releaseHeldButton]);

  // Safety: a held button must not stay pressed if the tab is hidden or loses
  // focus (otherwise the guest can be left with a stuck mouse button).
  useEffect(() => {
    if (!isActive) return;
    const release = () => { if (isHoldingRef.current) releaseHeldButton(); };
    const onVisibility = () => { if (document.visibilityState === "hidden") release(); };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("blur", release);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("blur", release);
    };
  }, [isActive, releaseHeldButton]);

  // Reset hold mode when click mode changes.
  useEffect(() => {
    if (isHoldingRef.current) releaseHeldButton();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clickMode]);

  // === RENDER ===
  if (!isActive) return null;

  const isServer = mode === "server";

  return (
    <div ref={containerRef} className={cn("absolute inset-0 z-40 pointer-events-none", className)}>
      {/* Control panel */}
      <div className="absolute top-2 left-1/2 -translate-x-1/2 pointer-events-auto z-50">
        <div className="flex items-center gap-1 bg-black/80 backdrop-blur-sm rounded-full px-2 py-1.5 shadow-lg border border-white/20">
          <Button variant="ghost" size="sm" className={cn("h-9 px-3 rounded-full text-xs font-medium transition-all hover:bg-white/20", clickMode === "left" ? "bg-blue-500 text-white hover:bg-blue-600" : "text-white/70")} onClick={() => setClickMode("left")}>
            <MousePointer2 className="h-4 w-4 mr-1.5" />Left
          </Button>
          <Button variant="ghost" size="sm" className={cn("h-9 px-3 rounded-full text-xs font-medium transition-all hover:bg-white/20", clickMode === "right" ? "bg-orange-500 text-white hover:bg-orange-600" : "text-white/70")} onClick={() => setClickMode("right")}>
            <MousePointer2 className="h-4 w-4 mr-1.5 scale-x-[-1]" />Right
          </Button>

          {/* Double-click */}
          <Button variant="ghost" size="icon" className="h-9 w-9 rounded-full text-white/70 hover:text-white hover:bg-white/20" title="Double-click" onClick={doDoubleClick}>
            <MousePointerClick className="h-4 w-4" />
          </Button>

          <div className="w-px h-6 bg-white/30 mx-1" />

          {/* Scroll */}
          <Button variant="ghost" size="icon" className="h-9 w-9 rounded-full text-white/70 hover:text-white hover:bg-white/20" title="Scroll up" onClick={() => doScroll("up")}>
            <ChevronUp className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" className="h-9 w-9 rounded-full text-white/70 hover:text-white hover:bg-white/20" title="Scroll down" onClick={() => doScroll("down")}>
            <ChevronDown className="h-4 w-4" />
          </Button>

          <div className="w-px h-6 bg-white/30 mx-1" />

          {/* Hold (window drag) */}
          <Button variant="ghost" size="sm" className={cn("h-9 px-3 rounded-full text-xs font-medium transition-all hover:bg-white/20", isHoldMode ? "bg-green-500 text-white hover:bg-green-600 animate-pulse" : "text-white/70")} onClick={toggleHoldMode}>
            {isHoldMode ? <Lock className="h-4 w-4 mr-1.5" /> : <Unlock className="h-4 w-4 mr-1.5" />}Hold
          </Button>

          <div className="w-px h-6 bg-white/30 mx-1" />

          <Button variant="ghost" size="icon" className="h-8 w-8 rounded-full text-white/70 hover:text-white hover:bg-white/20" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Draggable cursor */}
      <div
        ref={cursorRef}
        className={cn("absolute pointer-events-auto w-12 h-12 flex items-center justify-center cursor-grab active:cursor-grabbing select-none", isDragging && "scale-110")}
        style={{ left: position.x, top: position.y, touchAction: "none", transition: isDragging ? "none" : "transform 0.1s ease-out" }}
        onMouseDown={handleMouseDown}
        onTouchStart={handleTouchStart}
      >
        <div className={cn(
          "relative w-11 h-11 rounded-full flex items-center justify-center shadow-xl border-2 transition-all duration-100",
          isHoldMode ? "bg-green-500 border-green-300 ring-2 ring-green-400 ring-offset-1" : clickMode === "left" ? "bg-blue-500 border-blue-300" : "bg-orange-500 border-orange-300",
          showClickFeedback && "scale-90 brightness-125",
        )}>
          {isHoldMode ? <Lock className="h-5 w-5 text-white drop-shadow-md" /> : <MousePointer2 className={cn("h-5 w-5 text-white drop-shadow-md", clickMode === "right" && "scale-x-[-1]")} />}
          <div className={cn("absolute -bottom-1 -right-1 w-5 h-5 rounded-full shadow flex items-center justify-center", isHoldMode ? "bg-green-200 dark:bg-green-800" : "bg-white dark:bg-gray-200")}>
            <span className={cn("text-[10px] font-bold", isHoldMode ? "text-green-700 dark:text-green-300" : "text-gray-800 dark:text-gray-900")}>{isHoldMode ? "H" : clickMode === "left" ? "L" : "R"}</span>
          </div>
          <div className="absolute -top-1 -left-1 w-4 h-4 rounded-full bg-white/90 dark:bg-gray-200/90 shadow flex items-center justify-center">
            <Move className="h-2.5 w-2.5 text-gray-600 dark:text-gray-700" />
          </div>
        </div>
        {showClickFeedback && <div className="absolute inset-0 rounded-full animate-ping bg-white/40" />}
        {isHoldMode && <div className="absolute inset-0 rounded-full animate-pulse bg-green-400/30" />}
      </div>

      {/* Crosshair at cursor tip (the click point in client mode) */}
      <div className="absolute pointer-events-none" style={{ left: position.x + 20, top: position.y + 8, transform: "translate(-50%, -50%)" }}>
        <div className={cn("w-3 h-3 rounded-full border-2", isHoldMode ? "border-green-400 bg-green-400/50" : clickMode === "left" ? "border-blue-400" : "border-orange-400", !isHoldMode && "bg-white/30", isServer && "opacity-40")} />
      </div>

      {/* Instructions */}
      <div className="absolute bottom-3 left-1/2 -translate-x-1/2 pointer-events-none">
        <div className={cn("text-white/90 text-xs px-3 py-1.5 rounded-full", isHoldMode ? "bg-green-600/80" : "bg-black/70")}>
          {isHoldMode
            ? "Hold Mode ON • Drag to move window • Tap Hold to release"
            : isServer
              ? `Trackpad mode • Drag to move pointer • Tap to ${clickMode}-click`
              : `Drag to move • Tap to ${clickMode}-click`}
        </div>
      </div>
    </div>
  );
}
