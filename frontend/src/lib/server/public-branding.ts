import type { PublicBranding } from "@/types/branding";

// note: server components only (the root layout's metadata and the auth layout). It reads the
// Docker-internal backend address, which the browser has no route to.
const BACKEND_URL =
  process.env.INTERNAL_BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:4050";

// The sign-in page must render even when the backend is slow or down, so a branding read that
// has not answered by then is treated as "no branding" and the built-in look is shown.
const DEADLINE_MS = 1_500;
const REVALIDATE_SECONDS = 60;

const HEX = /^#[0-9a-fA-F]{6}$/;

// The backend caps and cleans these on write (services/branding/validation.py).
function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** A site path or an https Cloudinary URL, the only image sources the backend accepts. */
function imageUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  if (value.startsWith("/") && !value.startsWith("//")) return value;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname.endsWith(".cloudinary.com") ? value : null;
  } catch {
    return null;
  }
}

export function parsePublicBranding(body: unknown): PublicBranding | null {
  if (!body || typeof body !== "object") return null;
  const raw = body as Record<string, unknown>;
  if (typeof raw.version !== "string") return null;
  return {
    app_name: text(raw.app_name),
    tagline: text(raw.tagline),
    footer_text: text(raw.footer_text),
    accent_color: typeof raw.accent_color === "string" && HEX.test(raw.accent_color) ? raw.accent_color : null,
    theme: raw.theme === "light" || raw.theme === "dark" || raw.theme === "system" ? raw.theme : null,
    logo_light: imageUrl(raw.logo_light),
    logo_dark: imageUrl(raw.logo_dark),
    favicon: imageUrl(raw.favicon),
    version: raw.version,
  };
}

async function read(): Promise<PublicBranding | null> {
  try {
    const response = await fetch(`${BACKEND_URL}/api/v1/public/branding`, {
      next: { revalidate: REVALIDATE_SECONDS },
    });
    return response.ok ? parsePublicBranding(await response.json()) : null;
  } catch {
    return null;
  }
}

/** The deployment default branding, or null when it is unset or did not answer in time. */
export async function getPublicBranding(): Promise<PublicBranding | null> {
  // note: the deadline races the read instead of aborting it, so a slow answer still lands in
  // the data cache for the next request rather than being thrown away.
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), DEADLINE_MS);
  });
  try {
    return await Promise.race([read(), deadline]);
  } finally {
    clearTimeout(timer);
  }
}
