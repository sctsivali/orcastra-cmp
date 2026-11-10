/**
 * The image sources branding may use, checked again where they are shown.
 *
 * The backend validates every write (services/branding/validation.py), but a stored row may
 * predate that check, and an image source ends up in a CSS url(), a favicon link and an <img>.
 * A value is used only if it is an inline image, a path on this site made of plain characters,
 * or an https Cloudinary URL; anything else is shown as no image.
 */

const DATA_IMAGE = /^data:image\/(?:png|jpeg|webp|svg\+xml|x-icon|vnd\.microsoft\.icon);base64,[A-Za-z0-9+/=\s]+$/;
// A version query is allowed for the deployment's own asset links (/api/v1/public/branding/assets/...?v=).
const SITE_PATH = /^\/[A-Za-z0-9._~-][A-Za-z0-9._~/-]*(?:\?v=[A-Za-z0-9]+)?$/;
const URL_CHARS = /^[A-Za-z0-9._~:/?#[\]@!$&*+,;=%-]+$/;

export function safeImageSrc(value: unknown): string | null {
  if (typeof value !== "string" || !value) return null;
  if (DATA_IMAGE.test(value)) return value;
  if (value.length <= 512 && SITE_PATH.test(value)) return value;
  if (value.length > 1024 || !URL_CHARS.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname.endsWith(".cloudinary.com") ? value : null;
  } catch {
    return null;
  }
}

/** A CSS url() for a checked source, quoted. */
export function cssImageUrl(value: unknown): string | null {
  const src = safeImageSrc(value);
  return src ? `url("${src}")` : null;
}
