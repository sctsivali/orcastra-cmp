// Clipboard writes that also work outside a secure context.
//
// `navigator.clipboard` is secure-context only, so on a plain-HTTP LAN deployment
// it is undefined and `navigator.clipboard.writeText(...)` throws. This is the
// shared guard, with an execCommand fallback.
//
// It reports success instead of raising and shows no toast of its own: call sites
// word their feedback differently, and some also flip an icon on success.

function fallbackCopy(text: string): boolean {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  // Offscreen but focusable. A display:none or visibility:hidden node cannot be selected.
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  try {
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    // Deprecated, but the only programmatic copy available over plain HTTP.
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    // Always remove the node, including when execCommand throws.
    textarea.remove();
  }
}

/**
 * Copy text to the system clipboard. Resolves true on success, false on failure.
 * Never throws, so callers can branch on the result instead of wrapping in try/catch.
 */
export async function copyText(text: string): Promise<boolean> {
  if (typeof document === "undefined") return false;

  if (typeof navigator !== "undefined" && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permission denied or a transient failure. The textarea path often still works.
      return fallbackCopy(text);
    }
  }

  return fallbackCopy(text);
}
