/**
 * On-Screen Cursor - Virtual cursor for mobile/touch devices
 * Allows users to position a cursor and trigger left/right clicks.
 * Designed for mobile devices where right-click is not easily accessible.
 * 
 * Features:
 * - Left/Right click mode selection
 * - Hold Mode: keeps mouse button pressed for dragging windows in VM
 * - Tap to click, drag to move cursor
 * 
 * Implementation Notes:
 * - Uses refs for all state accessed in event callbacks to avoid stale closures
 * - Position is tracked both in state (for rendering) and refs (for callbacks)
 * - Cursor tip offset: the visible cursor tip is offset from cursor center
 * - Touch/mouse events are handled globally to prevent issues when finger/cursor leaves element
 */

"use client";

import * as React from "react";
import { useState, useRef, useCallback, useEffect } from "react";
import { cn } from "@/lib/utils";
import { MousePointer2, X, Move, Lock, Unlock } from "lucide-react";
import { Button } from "@/components/ui/button";

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

// Cursor tip offset from the cursor element's top-left corner
// This should match the visual crosshair position
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

  // === REFS (for callbacks to avoid stale closures) ===
  const containerRef = useRef<HTMLDivElement>(null);
  const cursorRef = useRef<HTMLDivElement>(null);
  
  // Mirror state in refs for callback access
  const positionRef = useRef<Position>({ x: 150, y: 150 });
  const isDraggingRef = useRef(false);
  const isHoldModeRef = useRef(false);
  const isHoldingRef = useRef(false);
  const clickModeRef = useRef<ClickMode>("left");
  
  // Drag tracking refs
  // Offset-based approach: store the offset between mouse position and cursor position at drag start
  // Then cursor position = mouse position - offset (clamped to bounds)
  const dragOffsetRef = useRef<Position>({ x: 0, y: 0 }); // offset from cursor top-left to mouse position
  const dragStartTimeRef = useRef(0);
  const dragStartClientRef = useRef<Position>({ x: 0, y: 0 }); // for tap detection

  // === SYNC STATE TO REFS ===
  useEffect(() => {
    positionRef.current = position;
  }, [position]);

  useEffect(() => {
    clickModeRef.current = clickMode;
  }, [clickMode]);

  useEffect(() => {
    isHoldModeRef.current = isHoldMode;
  }, [isHoldMode]);

  useEffect(() => {
    isHoldingRef.current = isHolding;
  }, [isHolding]);

  // === CANVAS ACCESS ===
  const getCanvas = useCallback((): HTMLCanvasElement | null => {
    const screen = document.getElementById(screenId);
    if (!screen) return null;
    return screen.querySelector("canvas");
  }, [screenId]);

  /**
   * Calculate the cursor tip position in screen coordinates
   * The tip is where clicks should be registered (the crosshair position)
   */
  const getTipScreenPosition = useCallback((cursorPos: Position): Position | null => {
    const container = containerRef.current;
    if (!container) return null;
    
    const containerRect = container.getBoundingClientRect();
    
    return {
      x: containerRect.left + cursorPos.x + CURSOR_TIP_OFFSET_X,
      y: containerRect.top + cursorPos.y + CURSOR_TIP_OFFSET_Y,
    };
  }, []);

  // === SPICE MOUSE EVENTS ===
  
  /**
   * Send mousemove event to canvas at the given screen coordinates
   * Used during hold mode drag to move the cursor in the VM
   */
  const sendMouseMoveToCanvas = useCallback((screenX: number, screenY: number) => {
    const canvas = getCanvas();
    if (!canvas) return;

    const sc = (canvas as any).sc;
    if (!sc?.inputs || sc.inputs.state !== "ready") return;

    const rect = canvas.getBoundingClientRect();
    
    // Clamp coordinates to canvas bounds
    const clampedX = Math.max(rect.left, Math.min(rect.right, screenX));
    const clampedY = Math.max(rect.top, Math.min(rect.bottom, screenY));

    // When holding, we need to send the correct button state
    const mode = clickModeRef.current;
    const buttonState = isHoldingRef.current ? (mode === "left" ? 1 : 2) : 0;

    const event = new PointerEvent("pointermove", {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: clampedX,
      clientY: clampedY,
      button: 0,
      buttons: buttonState,
    });
    canvas.dispatchEvent(event);
  }, [getCanvas]);

  /**
   * Send mousedown event to canvas
   */
  const sendMouseDown = useCallback((screenX: number, screenY: number): boolean => {
    const canvas = getCanvas();
    if (!canvas) return false;

    const sc = (canvas as any).sc;
    if (!sc?.inputs || sc.inputs.state !== "ready") return false;

    const rect = canvas.getBoundingClientRect();
    if (screenX < rect.left || screenX > rect.right || 
        screenY < rect.top || screenY > rect.bottom) {
      return false;
    }

    const mode = clickModeRef.current;
    const button = mode === "left" ? 0 : 2;
    const buttons = mode === "left" ? 1 : 2;

    // First position the cursor with a plain mousemove
    const moveEvent = new PointerEvent("pointermove", {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: screenX,
      clientY: screenY,
      button: 0,
      buttons: 0,
    });
    canvas.dispatchEvent(moveEvent);

    // Then mousedown
    const downEvent = new PointerEvent("pointerdown", {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: screenX,
      clientY: screenY,
      button,
      buttons,
    });
    canvas.dispatchEvent(downEvent);

    return true;
  }, [getCanvas]);

  /**
   * Send mouseup event to canvas
   */
  const sendMouseUp = useCallback((screenX: number, screenY: number) => {
    const canvas = getCanvas();
    if (!canvas) return;

    const mode = clickModeRef.current;
    const button = mode === "left" ? 0 : 2;

    const upEvent = new PointerEvent("pointerup", {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: screenX,
      clientY: screenY,
      button,
      buttons: 0,
    });
    canvas.dispatchEvent(upEvent);
  }, [getCanvas]);

  /**
   * Send complete click (down + up) sequence
   */
  const sendClick = useCallback(() => {
    const tipPos = getTipScreenPosition(positionRef.current);
    if (!tipPos) return;

    const canvas = getCanvas();
    if (!canvas) return;

    const sc = (canvas as any).sc;
    if (!sc?.inputs || sc.inputs.state !== "ready") return;

    const rect = canvas.getBoundingClientRect();
    if (tipPos.x < rect.left || tipPos.x > rect.right || 
        tipPos.y < rect.top || tipPos.y > rect.bottom) {
      return;
    }

    // Show feedback
    setShowClickFeedback(true);
    setTimeout(() => setShowClickFeedback(false), 150);

    const mode = clickModeRef.current;
    const button = mode === "left" ? 0 : 2;
    const buttons = mode === "left" ? 1 : 2;

    // Move -> Down -> Up sequence
    const moveEvent = new PointerEvent("pointermove", {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: tipPos.x,
      clientY: tipPos.y,
      button: 0,
      buttons: 0,
    });
    canvas.dispatchEvent(moveEvent);

    setTimeout(() => {
      const downEvent = new PointerEvent("pointerdown", {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: tipPos.x,
        clientY: tipPos.y,
        button,
        buttons,
      });
      canvas.dispatchEvent(downEvent);

      setTimeout(() => {
        const upEvent = new PointerEvent("pointerup", {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: tipPos.x,
          clientY: tipPos.y,
          button,
          buttons: 0,
        });
        canvas.dispatchEvent(upEvent);

        // Right click also needs contextmenu
        if (mode === "right") {
          const contextEvent = new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: tipPos.x,
            clientY: tipPos.y,
            button: 2,
          });
          canvas.dispatchEvent(contextEvent);
        }
      }, 30);
    }, 20);
  }, [getCanvas, getTipScreenPosition]);

  // === HOLD MODE TOGGLE ===
  const toggleHoldMode = useCallback(() => {
    const currentHoldMode = isHoldModeRef.current;
    const currentHolding = isHoldingRef.current;
    const currentPos = positionRef.current;
    const tipPos = getTipScreenPosition(currentPos);

    if (currentHoldMode) {
      // Turning off: release mouse at current tip position
      if (currentHolding && tipPos) {
        sendMouseUp(tipPos.x, tipPos.y);
      }
      setIsHolding(false);
      isHoldingRef.current = false;
      setIsHoldMode(false);
      isHoldModeRef.current = false;
    } else {
      // Turning on: send mousedown at current tip position
      if (tipPos) {
        const success = sendMouseDown(tipPos.x, tipPos.y);
        if (success) {
          setIsHolding(true);
          isHoldingRef.current = true;
          setIsHoldMode(true);
          isHoldModeRef.current = true;
        }
      }
    }
  }, [getTipScreenPosition, sendMouseDown, sendMouseUp]);

  // === CURSOR POSITION UPDATE ===
  /**
   * Update cursor position and send SPICE events if in hold mode
   * 
   * Uses offset-based positioning for smooth, predictable movement:
   * - At drag start, we calculate offset = mousePos - cursorPos
   * - During drag, cursorPos = mousePos - offset
   * 
   * This approach is more stable than delta-based tracking because:
   * - No accumulation of floating point errors
   * - No dependency on previous frame's position
   * - Cursor follows mouse exactly, even if frames are dropped
   */
  const positionFrameRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (positionFrameRef.current !== null) cancelAnimationFrame(positionFrameRef.current);
  }, []);

  const updateCursorPosition = useCallback((clientX: number, clientY: number) => {
    const container = containerRef.current;
    if (!container) return;
    
    // IMPORTANT: Only update if actually dragging
    // This is the primary guard against unwanted movement
    if (!isDraggingRef.current) return;

    const containerRect = container.getBoundingClientRect();
    
    // Calculate cursor position relative to container
    // mousePos (relative to container) = clientPos - containerRect.left/top
    // cursorPos = mousePos - dragOffset
    const mouseRelativeX = clientX - containerRect.left;
    const mouseRelativeY = clientY - containerRect.top;
    
    const newX = mouseRelativeX - dragOffsetRef.current.x;
    const newY = mouseRelativeY - dragOffsetRef.current.y;

    // Clamp to container bounds
    const cursorSize = 48;
    const maxX = containerRect.width - cursorSize;
    const maxY = containerRect.height - cursorSize;
    
    const clampedX = Math.max(0, Math.min(maxX, newX));
    const clampedY = Math.max(0, Math.min(maxY, newY));

    const newPos = { x: clampedX, y: clampedY };

    // The ref updates on every move; the render at most once a frame (it used to re-render the
    // whole overlay on every pointer event).
    positionRef.current = newPos;
    if (positionFrameRef.current === null) {
      positionFrameRef.current = requestAnimationFrame(() => {
        positionFrameRef.current = null;
        setPosition(positionRef.current);
      });
    }

    // If holding, send mousemove to SPICE for window dragging
    if (isHoldingRef.current) {
      const tipScreenX = containerRect.left + clampedX + CURSOR_TIP_OFFSET_X;
      const tipScreenY = containerRect.top + clampedY + CURSOR_TIP_OFFSET_Y;
      sendMouseMoveToCanvas(tipScreenX, tipScreenY);
    }
  }, [sendMouseMoveToCanvas]);

  // === DRAG START ===
  /**
   * Start dragging the on-screen cursor
   * Calculates the offset between mouse position and cursor position
   */
  const startDrag = useCallback((clientX: number, clientY: number) => {
    const container = containerRef.current;
    if (!container) return;
    
    const containerRect = container.getBoundingClientRect();
    
    // Calculate mouse position relative to container
    const mouseRelativeX = clientX - containerRect.left;
    const mouseRelativeY = clientY - containerRect.top;
    
    // Calculate offset: how far is the mouse from the cursor's top-left?
    // offset = mousePos - cursorPos
    const currentPos = positionRef.current;
    dragOffsetRef.current = {
      x: mouseRelativeX - currentPos.x,
      y: mouseRelativeY - currentPos.y,
    };
    
    // Store for tap detection
    dragStartClientRef.current = { x: clientX, y: clientY };
    dragStartTimeRef.current = Date.now();
    
    // Set dragging state - MUST be set AFTER storing offset
    isDraggingRef.current = true;
    setIsDragging(true);
  }, []);

  // === DRAG END ===
  /**
   * End dragging and detect taps vs drags
   */
  const endDrag = useCallback((clientX: number, clientY: number) => {
    if (!isDraggingRef.current) return;

    // Calculate movement and duration for tap detection
    const duration = Date.now() - dragStartTimeRef.current;
    
    // Use screen coordinates directly for tap detection
    const deltaX = Math.abs(clientX - dragStartClientRef.current.x);
    const deltaY = Math.abs(clientY - dragStartClientRef.current.y);
    const movement = Math.sqrt(deltaX * deltaX + deltaY * deltaY);

    // Clear dragging state FIRST to prevent any further updates
    isDraggingRef.current = false;
    setIsDragging(false);

    // Tap detection: short duration, minimal movement, NOT in hold mode
    if (duration < 250 && movement < 15 && !isHoldModeRef.current) {
      sendClick();
    }
  }, [sendClick]);

  // === GLOBAL EVENT LISTENERS ===
  useEffect(() => {
    if (!isActive) return;

    // Mouse move handler
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      e.preventDefault();
      updateCursorPosition(e.clientX, e.clientY);
    };

    // Mouse up handler
    const handleMouseUp = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      endDrag(e.clientX, e.clientY);
    };

    // Touch move handler
    const handleTouchMove = (e: TouchEvent) => {
      if (!isDraggingRef.current) return;
      if (e.touches.length !== 1) return;
      
      // Prevent scrolling while dragging
      e.preventDefault();
      
      const touch = e.touches[0];
      updateCursorPosition(touch.clientX, touch.clientY);
    };

    // Touch end handler
    const handleTouchEnd = (e: TouchEvent) => {
      if (!isDraggingRef.current) return;
      if (e.changedTouches.length < 1) return;
      
      // The browser follows a tap with compatibility mousedown/mouseup, which re-entered the drag
      // handlers and clicked the guest twice. Cancelling the touchend suppresses them.
      if (e.cancelable) e.preventDefault();
      const touch = e.changedTouches[0];
      endDrag(touch.clientX, touch.clientY);
    };

    // Touch cancel handler
    const handleTouchCancel = () => {
      if (!isDraggingRef.current) return;
      // End drag immediately
      isDraggingRef.current = false;
      setIsDragging(false);
    };

    // Add all listeners
    // Using document instead of window for better touch capture
    document.addEventListener("mousemove", handleMouseMove, { passive: false });
    document.addEventListener("mouseup", handleMouseUp);
    document.addEventListener("touchmove", handleTouchMove, { passive: false });
    document.addEventListener("touchend", handleTouchEnd);
    document.addEventListener("touchcancel", handleTouchCancel);

    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.removeEventListener("touchmove", handleTouchMove);
      document.removeEventListener("touchend", handleTouchEnd);
      document.removeEventListener("touchcancel", handleTouchCancel);
    };
  }, [isActive, updateCursorPosition, endDrag]);

  // === ELEMENT EVENT HANDLERS ===
  
  // Mouse down on cursor element
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    startDrag(e.clientX, e.clientY);
  }, [startDrag]);

  // Touch start on cursor element
  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    // Don't prevent default here - let the global handler do it during move
    e.stopPropagation();
    if (e.touches.length === 1) {
      startDrag(e.touches[0].clientX, e.touches[0].clientY);
    }
  }, [startDrag]);

  // === LIFECYCLE EFFECTS ===
  
  // Center cursor when activated
  useEffect(() => {
    if (isActive && containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      const centerPos = {
        x: rect.width / 2 - 24,
        y: rect.height / 2 - 24,
      };
      positionRef.current = centerPos;
      setPosition(centerPos);
    }
  }, [isActive]);

  // Cleanup: release hold mode when deactivated
  useEffect(() => {
    if (!isActive && isHoldingRef.current) {
      const tipPos = getTipScreenPosition(positionRef.current);
      if (tipPos) {
        sendMouseUp(tipPos.x, tipPos.y);
      }
      setIsHolding(false);
      isHoldingRef.current = false;
      setIsHoldMode(false);
      isHoldModeRef.current = false;
    }
  }, [isActive, getTipScreenPosition, sendMouseUp]);

  // Reset hold mode when click mode changes
  useEffect(() => {
    if (isHoldingRef.current) {
      const tipPos = getTipScreenPosition(positionRef.current);
      if (tipPos) {
        sendMouseUp(tipPos.x, tipPos.y);
      }
      setIsHolding(false);
      isHoldingRef.current = false;
      setIsHoldMode(false);
      isHoldModeRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clickMode]);

  // === RENDER ===
  
  if (!isActive) return null;

  return (
    <div
      ref={containerRef}
      className={cn("absolute inset-0 z-40 pointer-events-none", className)}
    >
      {/* Control panel - simple toggle buttons */}
      <div className="absolute top-2 left-1/2 -translate-x-1/2 pointer-events-auto z-50">
        <div className="flex items-center gap-1 bg-black/80 backdrop-blur-sm rounded-full px-2 py-1.5 shadow-lg border border-white/20">
          {/* Left Click Toggle */}
          <Button
            variant="ghost"
            size="sm"
            className={cn(
              "h-9 px-3 rounded-full text-xs font-medium transition-all",
              "hover:bg-white/20",
              clickMode === "left" 
                ? "bg-blue-500 text-white hover:bg-blue-600" 
                : "text-white/70"
            )}
            onClick={() => setClickMode("left")}
          >
            <MousePointer2 className="h-4 w-4 mr-1.5" />
            Left
          </Button>

          {/* Right Click Toggle */}
          <Button
            variant="ghost"
            size="sm"
            className={cn(
              "h-9 px-3 rounded-full text-xs font-medium transition-all",
              "hover:bg-white/20",
              clickMode === "right" 
                ? "bg-orange-500 text-white hover:bg-orange-600" 
                : "text-white/70"
            )}
            onClick={() => setClickMode("right")}
          >
            <MousePointer2 className="h-4 w-4 mr-1.5 scale-x-[-1]" />
            Right
          </Button>

          {/* Divider */}
          <div className="w-px h-6 bg-white/30 mx-1" />

          {/* Hold Mode Toggle - for dragging windows */}
          <Button
            variant="ghost"
            size="sm"
            className={cn(
              "h-9 px-3 rounded-full text-xs font-medium transition-all",
              "hover:bg-white/20",
              isHoldMode 
                ? "bg-green-500 text-white hover:bg-green-600 animate-pulse" 
                : "text-white/70"
            )}
            onClick={toggleHoldMode}
          >
            {isHoldMode ? (
              <Lock className="h-4 w-4 mr-1.5" />
            ) : (
              <Unlock className="h-4 w-4 mr-1.5" />
            )}
            Hold
          </Button>

          {/* Divider */}
          <div className="w-px h-6 bg-white/30 mx-1" />

          {/* Close Button */}
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 rounded-full text-white/70 hover:text-white hover:bg-white/20"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Draggable Cursor */}
      <div
        ref={cursorRef}
        className={cn(
          "absolute pointer-events-auto",
          "w-12 h-12 flex items-center justify-center",
          "cursor-grab active:cursor-grabbing",
          "select-none",
          isDragging && "scale-110"
        )}
        style={{
          // translate, not left/top: moving it composites instead of laying the page out again
          left: 0,
          top: 0,
          translate: `${position.x}px ${position.y}px`,
          touchAction: "none",
          transition: isDragging ? "none" : "transform 0.1s ease-out",
        }}
        onMouseDown={handleMouseDown}
        onTouchStart={handleTouchStart}
      >
        {/* Cursor Visual */}
        <div
          className={cn(
            "relative w-11 h-11 rounded-full",
            "flex items-center justify-center",
            "shadow-xl border-2",
            "transition-all duration-100",
            // Hold mode: green glow effect
            isHoldMode 
              ? "bg-green-500 border-green-300 ring-2 ring-green-400 ring-offset-1" 
              : clickMode === "left" 
                ? "bg-blue-500 border-blue-300" 
                : "bg-orange-500 border-orange-300",
            showClickFeedback && "scale-90 brightness-125"
          )}
        >
          {/* Cursor Icon - show Lock when holding */}
          {isHoldMode ? (
            <Lock className="h-5 w-5 text-white drop-shadow-md" />
          ) : (
            <MousePointer2 
              className={cn(
                "h-5 w-5 text-white drop-shadow-md",
                clickMode === "right" && "scale-x-[-1]"
              )} 
            />
          )}
          
          {/* Mode Label */}
          <div className={cn(
            "absolute -bottom-1 -right-1 w-5 h-5 rounded-full shadow flex items-center justify-center",
            isHoldMode ? "bg-status-online" : "bg-white dark:bg-gray-200"
          )}>
            <span className={cn(
              "text-[10px] font-bold",
              isHoldMode ? "text-status-online-foreground" : "text-gray-800 dark:text-gray-900"
            )}>
              {isHoldMode ? "H" : clickMode === "left" ? "L" : "R"}
            </span>
          </div>

          {/* Drag indicator */}
          <div className="absolute -top-1 -left-1 w-4 h-4 rounded-full bg-white/90 shadow flex items-center justify-center">
            <Move className="h-2.5 w-2.5 text-gray-600 dark:text-gray-700" />
          </div>
        </div>

        {/* Click Ripple Effect */}
        {showClickFeedback && (
          <div className="absolute inset-0 rounded-full animate-ping bg-white/40" />
        )}

        {/* Hold mode pulsing ring */}
        {isHoldMode && (
          <div className="absolute inset-0 rounded-full animate-pulse bg-status-online/30" />
        )}
      </div>

      {/* Crosshair at cursor tip */}
      <div
        className="absolute pointer-events-none"
        style={{
          left: 0,
          top: 0,
          translate: `${position.x + 20}px ${position.y + 8}px`,
          transform: "translate(-50%, -50%)",
        }}
      >
        <div className={cn(
          "w-3 h-3 rounded-full border-2",
          isHoldMode 
            ? "border-status-online bg-status-online/50" 
            : clickMode === "left" 
              ? "border-blue-400" 
              : "border-orange-400",
          !isHoldMode && "bg-white/30"
        )} />
      </div>

      {/* Instructions */}
      <div className="absolute bottom-3 left-1/2 -translate-x-1/2 pointer-events-none">
        <div className={cn(
          "text-white/90 text-xs px-3 py-1.5 rounded-full",
          // One pill for both modes: the words say which, and white on a bright green pill
          // measured below AA.
          "bg-black/70"
        )}>
          {isHoldMode 
            ? "Hold Mode ON • Drag to move window • Tap Hold to release"
            : `Drag to move • Tap to ${clickMode === "left" ? "left" : "right"}-click`
          }
        </div>
      </div>
    </div>
  );
}
