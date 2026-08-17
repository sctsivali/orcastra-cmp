// Clipboard writes that also work outside a secure context.
//
// `navigator.clipboard` is secure-context only, so on a plain-HTTP LAN deployment
// (ex; http://10.0.1.5) it is undefined and `navigator.clipboard.writeText(...)` throws.
// Three screens already carried their own copy of the guard plus an execCommand
// fallback, each subtly different, and two more had no fallback at all. This is the one
// implementation they all share now.
//
// It reports success instead of raising, and it raises no toast of its own, because the
// call sites word their feedback differently and two of them also flip an icon on
// success. Callers own their own messaging.

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
    // note: deprecated, and the only programmatic copy available over plain HTTP.
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
