// The base an integration should call, as the browser sees the backend.
//
// important: NEXT_PUBLIC_API_URL is the browser-visible backend origin, which is what belongs in
// a snippet an operator pastes into a client. The dashboard origin also works through the Next
// proxy, but every integration behind it would share one per-IP rate-limit bucket with every
// browser user.
//
// Trailing slashes are stripped because every caller appends `/api/v1/...`, and the backend
// answers `//api/v1/mcp` with 404. Null when unset, so a caller says so instead of guessing
// window.location.origin, which is the dashboard.
export const UNSET_PUBLIC_API_URL = "<your Orcastra API URL>";

export function publicApiBaseUrl(): string | null {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  const trimmed = configured ? configured.trim().replace(/\/+$/, "") : "";
  return trimmed || null;
}
