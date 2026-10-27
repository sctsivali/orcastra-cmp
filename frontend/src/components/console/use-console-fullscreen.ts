"use client";

import { useCallback, useEffect, useState } from "react";

type KeyboardLock = { lock?: (keys?: string[]) => Promise<void>; unlock?: () => void };

function keyboard(): KeyboardLock | undefined {
  return (navigator as Navigator & { keyboard?: KeyboardLock }).keyboard;
}

/**
 * Full screen for the console frame, with the keyboard captured where the browser allows it
 * (Chromium's Keyboard Lock API), so Win, Alt+Tab and Esc reach the guest instead of the host.
 * With the lock held, Esc goes to the guest and leaving takes a long press of Esc.
 */
export function useConsoleFullscreen(getTarget: () => HTMLElement | null) {
  const [supported, setSupported] = useState(false);
  const [keyboardLockSupported, setKeyboardLockSupported] = useState(false);
  const [active, setActive] = useState(false);
  const [keyboardLocked, setKeyboardLocked] = useState(false);

  useEffect(() => {
    // Read after mount: the server render has no document to agree with.
    setSupported(Boolean(document.fullscreenEnabled));
    setKeyboardLockSupported(typeof keyboard()?.lock === "function");
    const onChange = () => {
      const target = getTarget();
      const on = document.fullscreenElement !== null && document.fullscreenElement === target;
      setActive(on);
      if (!on) {
        keyboard()?.unlock?.();
        setKeyboardLocked(false);
      }
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      keyboard()?.unlock?.();
    };
  }, [getTarget]);

  const toggle = useCallback(async () => {
    if (document.fullscreenElement) {
      await document.exitFullscreen().catch(() => undefined);
      return;
    }
    const target = getTarget();
    if (!target) return;
    try {
      await target.requestFullscreen();
    } catch {
      return;
    }
    try {
      await keyboard()?.lock?.();
      setKeyboardLocked(Boolean(keyboard()?.lock));
    } catch {
      setKeyboardLocked(false);
    }
  }, [getTarget]);

  return { supported, keyboardLockSupported, active, keyboardLocked, toggle };
}
