/**
 * Reading the browser's Content Security Policy violation reports (app/api/csp-report/route.ts).
 *
 * The endpoint takes reports from anyone, so this module treats every field as hostile: it keeps
 * only what tells an operator which directive blocked what on which page, drops query strings and
 * fragments (they can carry tokens or personal data), strips control characters (no forged log
 * lines) and caps every string. The original policy is never kept: it carries the page's nonce.
 */

export const MAX_REPORT_BYTES = 8 * 1024;
export const CSP_REPORT_TYPES = ["application/csp-report", "application/reports+json"] as const;

export interface CspViolation {
  document: string;
  blocked: string;
  directive: string;
  disposition: string;
  source: string;
  line: number | null;
  column: number | null;
  sample: string;
}

function clean(value: unknown, max: number): string {
  if (typeof value !== "string") return "";
  // eslint-disable-next-line no-control-regex
  return value.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, "").slice(0, max);
}

/** A URL without its query and fragment; a CSP keyword (inline, eval, data) as it came. */
function withoutQuery(value: unknown): string {
  const text = clean(value, 2048);
  if (!text) return "";
  try {
    const url = new URL(text);
    if (url.protocol === "http:" || url.protocol === "https:" || url.protocol === "ws:" || url.protocol === "wss:") {
      return clean(`${url.origin}${url.pathname}`, 512);
    }
    return clean(url.protocol.replace(/:$/, ""), 32);
  } catch {
    return clean(text.split(/[?#]/)[0], 64);
  }
}

function whole(value: unknown): number | null {
  const n = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isInteger(n) && n >= 0 && n < 10_000_000 ? n : null;
}

function fromLegacy(body: Record<string, unknown>): CspViolation {
  return {
    document: withoutQuery(body["document-uri"]),
    blocked: withoutQuery(body["blocked-uri"]),
    directive: clean(body["effective-directive"] ?? body["violated-directive"], 64),
    disposition: clean(body.disposition, 16),
    source: withoutQuery(body["source-file"]),
    line: whole(body["line-number"]),
    column: whole(body["column-number"]),
    sample: clean(body["script-sample"], 40),
  };
}

function fromReporting(body: Record<string, unknown>): CspViolation {
  return {
    document: withoutQuery(body.documentURL),
    blocked: withoutQuery(body.blockedURL),
    directive: clean(body.effectiveDirective, 64),
    disposition: clean(body.disposition, 16),
    source: withoutQuery(body.sourceFile),
    line: whole(body.lineNumber),
    column: whole(body.columnNumber),
    sample: clean(body.sample, 40),
  };
}

/** The violations in a report body of either format; at most `limit`, never throws. */
export function parseCspReport(contentType: string, text: string, limit = 10): CspViolation[] {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return [];
  }
  const type = contentType.split(";")[0].trim().toLowerCase();
  if (type === "application/csp-report") {
    const body = (data as { "csp-report"?: unknown } | null)?.["csp-report"];
    return body && typeof body === "object" ? [fromLegacy(body as Record<string, unknown>)] : [];
  }
  if (type === "application/reports+json" && Array.isArray(data)) {
    return data
      .filter((r) => r && typeof r === "object" && (r as { type?: unknown }).type === "csp-violation")
      .map((r) => (r as { body?: unknown }).body)
      .filter((b): b is Record<string, unknown> => Boolean(b) && typeof b === "object")
      .slice(0, limit)
      .map(fromReporting);
  }
  return [];
}

/**
 * One budget for the whole process, so a flood of reports costs a bounded amount of logging
 * whoever sends it. `capacity` reports at once, refilled at `perSecond`.
 */
export function tokenBucket(capacity: number, perSecond: number, now: () => number = Date.now) {
  let tokens = capacity;
  let last = now();
  return {
    take(): boolean {
      const t = now();
      tokens = Math.min(capacity, tokens + ((t - last) / 1000) * perSecond);
      last = t;
      if (tokens < 1) return false;
      tokens -= 1;
      return true;
    },
  };
}

/** The body as text, or null once it passes `max` bytes; it never buffers more than that. */
export async function readCapped(body: ReadableStream<Uint8Array> | null, max = MAX_REPORT_BYTES): Promise<string | null> {
  if (!body) return "";
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    all.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(all);
}
