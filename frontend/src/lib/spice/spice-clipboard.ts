/**
 * Native bidirectional clipboard through the SPICE guest agent (spice-vdagent).
 *
 * The vendored main.js exposes two hooks on the connection that we fill in:
 *   - clipboard_on_guest_data(text): guest -> host (a guest Ctrl+C arrives here)
 *   - clipboard_get_host_data():     host -> guest (guest Ctrl+V asks for this)
 * plus clipboard_grab() to announce host clipboard so the guest can request it.
 *
 * No-op when the guest has no agent: the hooks simply never fire and grab
 * messages queue harmlessly. Browser clipboard access needs a secure context
 * (the app is served over HTTPS/wss) and may prompt for permission.
 */

type ConnGetter = () => any;

async function writeHostClipboard(text: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
  } catch {
    // permission denied / document not focused - best effort
  }
}

async function readHostClipboard(): Promise<string> {
  try {
    if (navigator.clipboard?.readText) return await navigator.clipboard.readText();
  } catch {
    // permission denied / document not focused
  }
  return "";
}

/**
 * Wire the SPICE connection to the browser clipboard. Returns a cleanup that
 * detaches the listeners and clears the hooks.
 */
export function enableAgentClipboard(getConn: ConnGetter): () => void {
  const sc = getConn();
  if (sc) {
    sc.clipboard_on_guest_data = (text: string) => {
      void writeHostClipboard(text);
    };
    sc.clipboard_get_host_data = () => readHostClipboard();
  }

  // When the user copies inside the browser while the console is open, tell the
  // guest the host has clipboard data so its next Ctrl+V can pull it.
  const onCopy = () => {
    const conn = getConn();
    if (conn && typeof conn.clipboard_grab === "function") {
      try {
        conn.clipboard_grab();
      } catch {
        /* ignore */
      }
    }
  };
  document.addEventListener("copy", onCopy);

  return () => {
    document.removeEventListener("copy", onCopy);
    const conn = getConn();
    if (conn) {
      conn.clipboard_on_guest_data = undefined;
      conn.clipboard_get_host_data = undefined;
    }
  };
}
