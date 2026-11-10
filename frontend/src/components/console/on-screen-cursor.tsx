/**
 * Virtual cursor for touch devices, where a right click or a held button is hard to produce.
 *
 * Dragging moves the cursor, a tap clicks with the selected button, and Hold keeps the button
 * pressed so a guest window can be dragged. State read inside event callbacks is mirrored in
 * refs to avoid stale closures, and move and end events are handled on the document so a drag
 * continues when the finger leaves the cursor.
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
  /** Id of the SPICE screen element whose canvas receives the events. */
  screenId: string;
  isActive: boolean;
  onClose?: () => void;
  className?: string;
}

// Where clicks land, measured from the cursor element's top-left corner. Must match the crosshair.
const CURSOR_TIP_OFFSET_X = 20;
const CURSOR_TIP_OFFSET_Y = 8;

export function OnScreenCursor({
  screenId,
  isActive,
  onClose,
  className,
}: OnScreenCursorProps) {
  const [clickMode, setClickMode] = useState<ClickMode>("left");
  const [position, setPosition] = useState<Position>({ x: 150, y: 150 });
  const [isDragging, setIsDragging] = useState(false);
  const [showClickFeedback, setShowClickFeedback] = useState(false);
  const [isHoldMode, setIsHoldMode] = useState(false);
  const [isHolding, setIsHolding] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);
  const cursorRef = useRef<HTMLDivElement>(null);
  
  // State mirrored in refs, so event callbacks never read a stale closure.
  const positionRef = useRef<Position>({ x: 150, y: 150 });
  const isDraggingRef = useRef(false);
  const isHoldModeRef = useRef(false);
  const isHoldingRef = useRef(false);
  const clickModeRef = useRef<ClickMode>("left");
  
  // The pointer's offset from the cursor's top-left at drag start; during a drag the cursor sits
  // at pointer - offset, clamped to the container.
  const dragOffsetRef = useRef<Position>({ x: 0, y: 0 });
  const dragStartTimeRef = useRef(0);
  const dragStartClientRef = useRef<Position>({ x: 0, y: 0 }); // for tap detection

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

  const getCanvas = useCallback((): HTMLCanvasElement | null => {
    const screen = document.getElementById(screenId);
    if (!screen) return null;
    return screen.querySelector("canvas");
  }, [screenId]);

  /** The cursor tip, where clicks land, in viewport coordinates. */
  const getTipScreenPosition = useCallback((cursorPos: Position): Position | null => {
    const container = containerRef.current;
    if (!container) return null;
    
    const containerRect = container.getBoundingClientRect();
    
    return {
      x: containerRect.left + cursorPos.x + CURSOR_TIP_OFFSET_X,
      y: containerRect.top + cursorPos.y + CURSOR_TIP_OFFSET_Y,
    };
  }, []);

  
  /** Moves the guest pointer, with the button held down while Hold is on. */
  const sendMouseMoveToCanvas = useCallback((screenX: number, screenY: number) => {
    const canvas = getCanvas();
    if (!canvas) return;

    const sc = (canvas as any).sc;
    if (!sc?.inputs || sc.inputs.state !== "ready") return;

    const rect = canvas.getBoundingClientRect();
    
    const clampedX = Math.max(rect.left, Math.min(rect.right, screenX));
    const clampedY = Math.max(rect.top, Math.min(rect.bottom, screenY));

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

    // Position the pointer first, then press.
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

    setShowClickFeedback(true);
    setTimeout(() => setShowClickFeedback(false), 150);

    const mode = clickModeRef.current;
    const button = mode === "left" ? 0 : 2;
    const buttons = mode === "left" ? 1 : 2;

    // Move, press and release, a few milliseconds apart.
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

        // A right click also dispatches contextmenu.
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

  const toggleHoldMode = useCallback(() => {
    const currentHoldMode = isHoldModeRef.current;
    const currentHolding = isHoldingRef.current;
    const currentPos = positionRef.current;
    const tipPos = getTipScreenPosition(currentPos);

    if (currentHoldMode) {
      // Turning off releases the button at the tip.
      if (currentHolding && tipPos) {
        sendMouseUp(tipPos.x, tipPos.y);
      }
      setIsHolding(false);
      isHoldingRef.current = false;
      setIsHoldMode(false);
      isHoldModeRef.current = false;
    } else {
      // Turning on presses it there.
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

  // The cursor is placed at pointer - offset rather than moved by deltas, so it follows the
  // pointer exactly with no accumulated error, even when frames are dropped.
  const positionFrameRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (positionFrameRef.current !== null) cancelAnimationFrame(positionFrameRef.current);
  }, []);

  const updateCursorPosition = useCallback((clientX: number, clientY: number) => {
    const container = containerRef.current;
    if (!container) return;
    
    if (!isDraggingRef.current) return;

    const containerRect = container.getBoundingClientRect();
    
    const mouseRelativeX = clientX - containerRect.left;
    const mouseRelativeY = clientY - containerRect.top;
    
    const newX = mouseRelativeX - dragOffsetRef.current.x;
    const newY = mouseRelativeY - dragOffsetRef.current.y;

    const cursorSize = 48;
    const maxX = containerRect.width - cursorSize;
    const maxY = containerRect.height - cursorSize;
    
    const clampedX = Math.max(0, Math.min(maxX, newX));
    const clampedY = Math.max(0, Math.min(maxY, newY));

    const newPos = { x: clampedX, y: clampedY };

    // The ref updates on every move; the overlay renders at most once a frame.
    positionRef.current = newPos;
    if (positionFrameRef.current === null) {
      positionFrameRef.current = requestAnimationFrame(() => {
        positionFrameRef.current = null;
        setPosition(positionRef.current);
      });
    }

    // While holding, the guest pointer follows, which drags the guest window.
    if (isHoldingRef.current) {
      const tipScreenX = containerRect.left + clampedX + CURSOR_TIP_OFFSET_X;
      const tipScreenY = containerRect.top + clampedY + CURSOR_TIP_OFFSET_Y;
      sendMouseMoveToCanvas(tipScreenX, tipScreenY);
    }
  }, [sendMouseMoveToCanvas]);

  const startDrag = useCallback((clientX: number, clientY: number) => {
    const container = containerRef.current;
    if (!container) return;
    
    const containerRect = container.getBoundingClientRect();
    
    const mouseRelativeX = clientX - containerRect.left;
    const mouseRelativeY = clientY - containerRect.top;
    
    const currentPos = positionRef.current;
    dragOffsetRef.current = {
      x: mouseRelativeX - currentPos.x,
      y: mouseRelativeY - currentPos.y,
    };
    
    dragStartClientRef.current = { x: clientX, y: clientY };
    dragStartTimeRef.current = Date.now();
    
    // Set after the offset, which a move reads as soon as this is true.
    isDraggingRef.current = true;
    setIsDragging(true);
  }, []);

  /** Ends a drag; a short one that barely moved is a tap and clicks. */
  const endDrag = useCallback((clientX: number, clientY: number) => {
    if (!isDraggingRef.current) return;

    const duration = Date.now() - dragStartTimeRef.current;
    
    const deltaX = Math.abs(clientX - dragStartClientRef.current.x);
    const deltaY = Math.abs(clientY - dragStartClientRef.current.y);
    const movement = Math.sqrt(deltaX * deltaX + deltaY * deltaY);

    // Cleared first, so no later move updates the position.
    isDraggingRef.current = false;
    setIsDragging(false);

    if (duration < 250 && movement < 15 && !isHoldModeRef.current) {
      sendClick();
    }
  }, [sendClick]);

  useEffect(() => {
    if (!isActive) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      e.preventDefault();
      updateCursorPosition(e.clientX, e.clientY);
    };

    const handleMouseUp = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      endDrag(e.clientX, e.clientY);
    };

    const handleTouchMove = (e: TouchEvent) => {
      if (!isDraggingRef.current) return;
      if (e.touches.length !== 1) return;
      
      // Prevent scrolling while dragging
      e.preventDefault();
      
      const touch = e.touches[0];
      updateCursorPosition(touch.clientX, touch.clientY);
    };

    const handleTouchEnd = (e: TouchEvent) => {
      if (!isDraggingRef.current) return;
      if (e.changedTouches.length < 1) return;
      
      // The browser follows a tap with compatibility mousedown/mouseup, which would re-enter the
      // drag handlers and click the guest twice. Cancelling the touchend suppresses them.
      if (e.cancelable) e.preventDefault();
      const touch = e.changedTouches[0];
      endDrag(touch.clientX, touch.clientY);
    };

    const handleTouchCancel = () => {
      if (!isDraggingRef.current) return;
      isDraggingRef.current = false;
      setIsDragging(false);
    };

    // On the document, so a drag continues when the finger leaves the cursor.
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

  
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    startDrag(e.clientX, e.clientY);
  }, [startDrag]);

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    // No preventDefault here; the document's touchmove handler does it during a drag.
    e.stopPropagation();
    if (e.touches.length === 1) {
      startDrag(e.touches[0].clientX, e.touches[0].clientY);
    }
  }, [startDrag]);

  
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

  // Deactivating releases a held button.
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

  // Switching the click mode releases a held button.
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

  
  if (!isActive) return null;

  return (
    <div
      ref={containerRef}
      className={cn("absolute inset-0 z-40 pointer-events-none", className)}
    >
      <div className="absolute top-2 left-1/2 -translate-x-1/2 pointer-events-auto z-50">
        <div className="flex items-center gap-1 bg-black/80 backdrop-blur-sm rounded-full px-2 py-1.5 shadow-lg border border-white/20">
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

          <div className="w-px h-6 bg-white/30 mx-1" />

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

          <div className="w-px h-6 bg-white/30 mx-1" />

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
          // translate rather than left/top, so a move composites instead of re-running layout
          left: 0,
          top: 0,
          translate: `${position.x}px ${position.y}px`,
          touchAction: "none",
          transition: isDragging ? "none" : "transform 0.1s ease-out",
        }}
        onMouseDown={handleMouseDown}
        onTouchStart={handleTouchStart}
      >
        <div
          className={cn(
            "relative w-11 h-11 rounded-full",
            "flex items-center justify-center",
            "shadow-xl border-2",
            "transition-all duration-100",
            isHoldMode 
              ? "bg-green-500 border-green-300 ring-2 ring-green-400 ring-offset-1" 
              : clickMode === "left" 
                ? "bg-blue-500 border-blue-300" 
                : "bg-orange-500 border-orange-300",
            showClickFeedback && "scale-90 brightness-125"
          )}
        >
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

          <div className="absolute -top-1 -left-1 w-4 h-4 rounded-full bg-white/90 shadow flex items-center justify-center">
            <Move className="h-2.5 w-2.5 text-gray-600 dark:text-gray-700" />
          </div>
        </div>

        {showClickFeedback && (
          <div className="absolute inset-0 rounded-full animate-ping bg-white/40" />
        )}

        {isHoldMode && (
          <div className="absolute inset-0 rounded-full animate-pulse bg-status-online/30" />
        )}
      </div>

      {/* Crosshair at the cursor tip; its offset matches CURSOR_TIP_OFFSET_X and _Y. */}
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

      <div className="absolute bottom-3 left-1/2 -translate-x-1/2 pointer-events-none">
        <div className={cn(
          "text-white/90 text-xs px-3 py-1.5 rounded-full",
          // One pill for both modes: the text names the mode, and white on bright green fails
          // AA contrast.
          "bg-black/70"
        )}>
          {isHoldMode 
            ? "Hold Mode ON · Drag to move window · Tap Hold to release"
            : `Drag to move · Tap to ${clickMode === "left" ? "left" : "right"}-click`
          }
        </div>
      </div>
    </div>
  );
}
