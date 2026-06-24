/**
 * On-Screen Keyboard - Virtual keyboard for console interaction.
 * Uses CSS with explicit pixel-based sizing calculated from container width.
 * 
 * KEY FIX for sizing: Uses ResizeObserver to measure actual container width
 * and calculates key sizes as exact pixels.
 * 
 * KEY FIX for mobile keyboard: Uses a stable input that doesn't lose focus
 * on parent re-renders by using refs and avoiding state in critical paths.
 */

"use client";

import * as React from "react";
import { useState, useCallback, useEffect, useRef, memo } from "react";
import { X, Keyboard } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface OnScreenKeyboardProps {
  onKeyPress: (key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }, skipFocus?: boolean) => void;
  onClose?: () => void;
  className?: string;
}

type KeyboardLayout = 'main' | 'symbols' | 'function' | 'navigation';

interface KeyConfig {
  key: string;
  label?: string;
  width?: number;
  special?: boolean;
}

// Layouts with consistent total width per row (14.5 units for main keyboard)
const MAIN_LAYOUT: KeyConfig[][] = [
  [
    { key: '`' }, { key: '1' }, { key: '2' }, { key: '3' }, { key: '4' }, { key: '5' },
    { key: '6' }, { key: '7' }, { key: '8' }, { key: '9' }, { key: '0' },
    { key: '-' }, { key: '=' }, { key: 'Backspace', label: '⌫', width: 1.5, special: true },
  ],
  [
    { key: 'Tab', label: 'Tab', width: 1.5, special: true },
    { key: 'q' }, { key: 'w' }, { key: 'e' }, { key: 'r' }, { key: 't' },
    { key: 'y' }, { key: 'u' }, { key: 'i' }, { key: 'o' }, { key: 'p' },
    { key: '[' }, { key: ']' }, { key: '\\' },
  ],
  [
    { key: 'CapsLock', label: 'Caps', width: 1.75, special: true },
    { key: 'a' }, { key: 's' }, { key: 'd' }, { key: 'f' }, { key: 'g' },
    { key: 'h' }, { key: 'j' }, { key: 'k' }, { key: 'l' }, { key: ';' }, { key: "'" },
    { key: 'Enter', label: '↵', width: 1.75, special: true },
  ],
  [
    { key: 'Shift', label: '⇧', width: 2.25, special: true },
    { key: 'z' }, { key: 'x' }, { key: 'c' }, { key: 'v' }, { key: 'b' },
    { key: 'n' }, { key: 'm' }, { key: ',' }, { key: '.' }, { key: '/' },
    { key: 'Shift', label: '⇧', width: 2.25, special: true },
  ],
  [
    { key: 'Control', label: 'Ctrl', width: 1.5, special: true },
    { key: 'Meta', label: 'Win', width: 1.25, special: true },
    { key: 'Alt', label: 'Alt', width: 1.25, special: true },
    { key: ' ', label: 'Space', width: 6, special: true },
    { key: 'Alt', label: 'Alt', width: 1.25, special: true },
    { key: 'Control', label: 'Ctrl', width: 1.5, special: true },
    { key: 'Escape', label: 'Esc', width: 1.25, special: true },
  ],
];

const FUNCTION_LAYOUT: KeyConfig[][] = [
  [
    { key: 'Escape', label: 'Esc', special: true },
    { key: 'F1' }, { key: 'F2' }, { key: 'F3' }, { key: 'F4' },
    { key: 'F5' }, { key: 'F6' }, { key: 'F7' }, { key: 'F8' },
    { key: 'F9' }, { key: 'F10' }, { key: 'F11' }, { key: 'F12' },
  ],
  [
    { key: 'PrintScreen', label: 'PrtSc', special: true },
    { key: 'ScrollLock', label: 'ScrLk', special: true },
    { key: 'Pause', special: true },
    { key: 'Insert', label: 'Ins', special: true },
    { key: 'Delete', label: 'Del', special: true },
    { key: 'Home', special: true },
    { key: 'End', special: true },
    { key: 'PageUp', label: 'PgUp', special: true },
    { key: 'PageDown', label: 'PgDn', special: true },
  ],
];

const NAVIGATION_LAYOUT: KeyConfig[][] = [
  [{ key: 'ArrowUp', label: '↑', width: 1, special: true }],
  [
    { key: 'ArrowLeft', label: '←', width: 1, special: true },
    { key: 'ArrowDown', label: '↓', width: 1, special: true },
    { key: 'ArrowRight', label: '→', width: 1, special: true },
  ],
];

const SYMBOLS_LAYOUT: KeyConfig[][] = [
  [
    { key: '!' }, { key: '@' }, { key: '#' }, { key: '$' }, { key: '%' },
    { key: '^' }, { key: '&' }, { key: '*' }, { key: '(' }, { key: ')' },
  ],
  [
    { key: '[' }, { key: ']' }, { key: '{' }, { key: '}' }, { key: '<' },
    { key: '>' }, { key: "'" }, { key: '"' }, { key: '`' }, { key: '~' },
  ],
  [
    { key: '.' }, { key: ',' }, { key: ':' }, { key: ';' }, { key: '?' },
    { key: '/' }, { key: '\\' }, { key: '|' }, { key: '_' }, { key: '-' },
  ],
  [
    { key: '+' }, { key: '=' }, { key: '*' },
    { key: ' ', label: 'Space', width: 4, special: true },
    { key: 'Backspace', label: '⌫', width: 2, special: true },
  ],
];

// Stable native input component - uses contentEditable for better mobile keyboard stability
// The key insight: contentEditable doesn't lose focus on value changes like input does
const NativeKeyboardInput = memo(function NativeKeyboardInput({
  onKeyPress,
  onToggleOff,
}: {
  onKeyPress: (key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }, skipFocus?: boolean) => void;
  onToggleOff: () => void;
}) {
  const editorRef = useRef<HTMLDivElement>(null);
  const isMountedRef = useRef(true);
  const isProcessingRef = useRef(false);
  const lastTextRef = useRef('');
  
  // Store callbacks in refs - this prevents re-renders when props change
  const onKeyPressRef = useRef(onKeyPress);
  const onToggleOffRef = useRef(onToggleOff);
  
  // Update refs when props change (sync update, no re-render triggered)
  useEffect(() => {
    onKeyPressRef.current = onKeyPress;
    onToggleOffRef.current = onToggleOff;
  });

  // Focus on mount and cleanup on unmount
  // Also attach beforeinput listener for mobile backspace handling
  useEffect(() => {
    isMountedRef.current = true;
    const timer = setTimeout(() => {
      if (isMountedRef.current && editorRef.current) {
        editorRef.current.focus();
      }
    }, 100);
    
    // beforeinput event is more reliable for mobile keyboards
    // It fires BEFORE the browser modifies content
    const handleBeforeInput = (e: InputEvent) => {
      if (e.inputType === 'deleteContentBackward') {
        e.preventDefault();
        e.stopPropagation();
        // Send backspace directly to VM
        onKeyPressRef.current('Backspace', undefined, true);
        // Clear any remaining content
        if (editorRef.current) {
          editorRef.current.textContent = '';
          lastTextRef.current = '';
        }
      } else if (e.inputType === 'deleteContentForward') {
        e.preventDefault();
        e.stopPropagation();
        // Send delete directly to VM
        onKeyPressRef.current('Delete', undefined, true);
        if (editorRef.current) {
          editorRef.current.textContent = '';
          lastTextRef.current = '';
        }
      }
    };
    
    const editor = editorRef.current;
    if (editor) {
      editor.addEventListener('beforeinput', handleBeforeInput);
    }
    
    return () => {
      isMountedRef.current = false;
      clearTimeout(timer);
      if (editor) {
        editor.removeEventListener('beforeinput', handleBeforeInput);
      }
    };
  }, []);

  // Handle input - track both additions AND deletions
  // Mobile keyboards with autocomplete don't fire beforeinput for prediction deletions
  const handleInput = useCallback(() => {
    if (isProcessingRef.current || !editorRef.current) return;
    
    const currentText = editorRef.current.textContent || '';
    const prevText = lastTextRef.current;

    isProcessingRef.current = true;
    // important: try/finally so the guard always clears - otherwise a throw mid-loop
    // (or an unmount) would wedge it true and silently drop all later input.
    try {
      if (currentText.length > prevText.length) {
        // Text was added - send new characters
        const newChars = currentText.slice(prevText.length);
        for (const char of newChars) {
          onKeyPressRef.current(char, undefined, true);
        }
      } else if (currentText.length < prevText.length) {
        // Text was deleted - send backspaces for each deleted char
        const deletedCount = prevText.length - currentText.length;
        for (let i = 0; i < deletedCount; i++) {
          onKeyPressRef.current('Backspace', undefined, true);
        }
      }
      // Note: if lengths are equal, it might be a replacement - handle if needed
    } finally {
      // Update tracked text (don't clear - we need to track for deletions)
      lastTextRef.current = currentText;
      isProcessingRef.current = false;
    }
  }, []);

  // Handle special keys
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    const specialKeys: Record<string, boolean> = {
      'Enter': true, 'Tab': true, 'Escape': true, 'Backspace': true, 'Delete': true,
      'ArrowUp': true, 'ArrowDown': true, 'ArrowLeft': true, 'ArrowRight': true,
    };
    
    if (specialKeys[e.key]) {
      e.preventDefault();
      e.stopPropagation();
      
      // For backspace/delete: clear any remaining content in field first
      // Mobile browsers can have internal buffers that aren't synced with textContent
      if ((e.key === 'Backspace' || e.key === 'Delete') && editorRef.current) {
        // Force clear any content before sending to VM
        editorRef.current.textContent = '';
        lastTextRef.current = '';
      }
      
      // Always send special keys directly to VM
      // Pass skipFocus: true for native keyboard
      onKeyPressRef.current(e.key, undefined, true);
    }
  }, []);

  const handleClose = useCallback(() => {
    onToggleOffRef.current();
  }, []);

  // Prevent buttons from stealing focus
  const preventFocusLoss = useCallback((e: React.MouseEvent | React.TouchEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  return (
    <div className="mb-1 p-1 bg-background/50 rounded border">
      <div className="flex items-center gap-1 relative">
        <div
          ref={editorRef}
          contentEditable
          suppressContentEditableWarning
          inputMode="text"
          role="textbox"
          aria-multiline="false"
          autoCorrect="off"
          autoCapitalize="off"
          spellCheck={false}
          data-gramm="false"
          data-gramm_editor="false"
          data-enable-grammarly="false"
          className="flex-1 h-8 px-2 text-sm bg-background border rounded focus:outline-none focus:ring-2 focus:ring-primary overflow-hidden whitespace-nowrap flex items-center"
          style={{ 
            minHeight: '32px', 
            lineHeight: '32px',
            color: 'transparent',  // Hide typed text
            caretColor: 'hsl(var(--foreground))', // But show cursor
          }}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
        />
        <div className="absolute left-3 text-muted-foreground text-sm pointer-events-none">
          Tap here to type...
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 px-2 text-xs"
          onMouseDown={preventFocusLoss}
          onTouchStart={preventFocusLoss}
          onClick={handleClose}
        >
          Close
        </Button>
      </div>
    </div>
  );
}, () => true); // Custom areEqual - ALWAYS return true to never re-render

export function OnScreenKeyboard({ onKeyPress, onClose, className }: OnScreenKeyboardProps) {
  const [activeLayout, setActiveLayout] = useState<KeyboardLayout>('main');
  const [showNativeKeyboard, setShowNativeKeyboard] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  
  const [modifiers, setModifiers] = useState({
    ctrl: false,
    alt: false,
    shift: false,
    meta: false,
    capsLock: false,
  });

  // Measure container width using ResizeObserver
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setContainerWidth(entry.contentRect.width);
      }
    });

    observer.observe(container);
    // Initial measurement
    setContainerWidth(container.clientWidth);

    return () => observer.disconnect();
  }, []);

  const handleKeyPress = useCallback((key: string) => {
    if (key === 'Control') { setModifiers(m => ({ ...m, ctrl: !m.ctrl })); return; }
    if (key === 'Alt') { setModifiers(m => ({ ...m, alt: !m.alt })); return; }
    if (key === 'Shift') { setModifiers(m => ({ ...m, shift: !m.shift })); return; }
    if (key === 'Meta') { setModifiers(m => ({ ...m, meta: !m.meta })); return; }
    if (key === 'CapsLock') { setModifiers(m => ({ ...m, capsLock: !m.capsLock })); return; }

    // important: CapsLock only inverts case for letters - it must NOT add Shift to
    // digits/symbols (CapsLock + "1" stays "1", not "!"). XOR(shift, capsLock) for
    // letters mirrors the key-cap label logic below so display and output agree.
    const isLetter = key.length === 1 && /[a-zA-Z]/.test(key);
    const effectiveShift = isLetter ? (modifiers.shift !== modifiers.capsLock) : modifiers.shift;

    onKeyPress(key, {
      ctrl: modifiers.ctrl,
      alt: modifiers.alt,
      shift: effectiveShift,
      meta: modifiers.meta,
    });

    setModifiers(m => ({ ...m, ctrl: false, alt: false, shift: false, meta: false }));
  }, [modifiers, onKeyPress]);

  // Calculate key sizes based on container width
  const renderLayout = useCallback((layout: KeyConfig[][]) => {
    if (containerWidth === 0) return null;

    // Find max row width
    const rowWidths = layout.map(row => row.reduce((sum, k) => sum + (k.width || 1), 0));
    const maxRowWidth = Math.max(...rowWidths);
    
    // Calculate available width (container - padding - gaps)
    const padding = 8; // 4px on each side
    const gap = 2;
    const availableWidth = containerWidth - padding;

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: `${gap}px`, width: '100%' }}>
        {layout.map((row, rowIndex) => {
          const rowWidth = rowWidths[rowIndex];
          const numKeys = row.length;
          const totalGapWidth = (numKeys - 1) * gap;
          const keyAreaWidth = availableWidth - totalGapWidth;
          
          return (
            <div 
              key={rowIndex} 
              style={{ 
                display: 'flex', 
                gap: `${gap}px`,
                width: '100%',
              }}
            >
              {row.map((config, keyIndex) => {
                const keyWidth = config.width || 1;
                // Calculate pixel width proportionally
                const pixelWidth = (keyWidth / rowWidth) * keyAreaWidth;
                
                const isModifierActive = 
                  (config.key === 'Control' && modifiers.ctrl) ||
                  (config.key === 'Alt' && modifiers.alt) ||
                  (config.key === 'Shift' && modifiers.shift) ||
                  (config.key === 'Meta' && modifiers.meta) ||
                  (config.key === 'CapsLock' && modifiers.capsLock);

                let displayLabel = config.label || config.key;
                if (config.key.length === 1 && config.key.match(/[a-z]/i)) {
                  displayLabel = (modifiers.shift !== modifiers.capsLock) 
                    ? displayLabel.toUpperCase() 
                    : displayLabel.toLowerCase();
                }

                return (
                  <button
                    key={`${config.key}-${keyIndex}`}
                    type="button"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => handleKeyPress(config.key)}
                    onTouchEnd={(e) => {
                      e.preventDefault();
                      handleKeyPress(config.key);
                    }}
                    className={cn(
                      "rounded font-medium transition-colors",
                      "border shadow-sm",
                      "hover:brightness-110 active:brightness-90",
                      "touch-manipulation select-none",
                      "flex items-center justify-center",
                      "overflow-hidden whitespace-nowrap",
                      // Regular keys: dark background with light text for visibility
                      config.special 
                        ? "bg-zinc-700 text-zinc-200 border-zinc-600" 
                        : "bg-zinc-800 text-zinc-100 border-zinc-600",
                      isModifierActive && "!bg-primary !text-primary-foreground !border-primary"
                    )}
                    style={{
                      width: `${pixelWidth}px`,
                      height: '28px',
                      fontSize: `${Math.max(7, Math.min(11, containerWidth / 80))}px`,
                      padding: '0 1px',
                      flexShrink: 0,
                      flexGrow: 0,
                    }}
                  >
                    {displayLabel}
                  </button>
                );
              })}
            </div>
          );
        })}
      </div>
    );
  }, [containerWidth, modifiers, handleKeyPress]);

  return (
    <div 
      ref={containerRef}
      className={cn(
        "bg-zinc-900 backdrop-blur-sm border-t border-zinc-700",
        "touch-manipulation",
        className
      )}
      style={{ 
        padding: '4px', 
        boxSizing: 'border-box',
        width: '100%',
        maxWidth: '100%',
        overflow: 'hidden',
      }}
      onMouseDown={(e) => e.stopPropagation()}
      onTouchStart={(e) => e.stopPropagation()}
    >
      {/* Layout tabs */}
      <div className="flex items-center justify-between mb-1 gap-1 flex-wrap">
        <div className="flex items-center gap-1 flex-wrap">
          <Button
            variant={showNativeKeyboard ? 'default' : 'ghost'}
            size="sm"
            className={cn(
              "h-6 px-2 text-[10px] sm:text-xs",
              !showNativeKeyboard && "text-zinc-300 hover:text-white hover:bg-zinc-700"
            )}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => setShowNativeKeyboard(prev => !prev)}
          >
            <Keyboard className="h-3 w-3 mr-1" />
            Native
          </Button>
          
          <div className="w-px h-4 bg-zinc-600 mx-1" />

          {(['main', 'symbols', 'function', 'navigation'] as const).map((layout) => (
            <Button
              key={layout}
              variant={activeLayout === layout && !showNativeKeyboard ? 'default' : 'ghost'}
              size="sm"
              className={cn(
                "h-6 px-2 text-[10px] sm:text-xs",
                (activeLayout !== layout || showNativeKeyboard) && "text-zinc-300 hover:text-white hover:bg-zinc-700"
              )}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                // When clicking a layout button, hide native keyboard and show selected layout
                setShowNativeKeyboard(false);
                setActiveLayout(layout);
              }}
            >
              {layout === 'main' ? 'ABC' : layout === 'symbols' ? '#+=' : layout === 'function' ? 'Fn' : '←→'}
            </Button>
          ))}
        </div>

        {onClose && (
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-zinc-300 hover:text-white hover:bg-zinc-700"
            onMouseDown={(e) => e.preventDefault()}
            onClick={onClose}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>

      {/* Native keyboard input - isolated component to prevent focus issues */}
      {showNativeKeyboard && (
        <NativeKeyboardInput 
          onKeyPress={onKeyPress}
          onToggleOff={() => setShowNativeKeyboard(false)}
        />
      )}

      {/* Keyboard - only show when native keyboard is OFF and we have container width */}
      {!showNativeKeyboard && containerWidth > 0 && (
        <div style={{ width: '100%', overflow: 'hidden' }}>
          {activeLayout === 'main' && renderLayout(MAIN_LAYOUT)}
          {activeLayout === 'symbols' && renderLayout(SYMBOLS_LAYOUT)}
          {activeLayout === 'function' && renderLayout(FUNCTION_LAYOUT)}
          {activeLayout === 'navigation' && renderLayout(NAVIGATION_LAYOUT)}
        </div>
      )}

      {/* Modifier status */}
      {(modifiers.ctrl || modifiers.alt || modifiers.shift || modifiers.meta) && (
        <div className="flex items-center justify-center gap-1 mt-1 text-[10px] text-zinc-400">
          <span>Active:</span>
          {modifiers.ctrl && <span className="px-1 py-0.5 bg-primary/30 text-primary-foreground rounded">Ctrl</span>}
          {modifiers.alt && <span className="px-1 py-0.5 bg-primary/30 text-primary-foreground rounded">Alt</span>}
          {modifiers.shift && <span className="px-1 py-0.5 bg-primary/30 text-primary-foreground rounded">Shift</span>}
          {modifiers.meta && <span className="px-1 py-0.5 bg-primary/30 text-primary-foreground rounded">Win</span>}
        </div>
      )}
    </div>
  );
}

export default OnScreenKeyboard;
