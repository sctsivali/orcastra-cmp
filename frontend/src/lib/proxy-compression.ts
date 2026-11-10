/**
 * Gzip for the browser leg of the /api/v1 proxy.
 *
 * important: Next's `compress: true` never reaches a route handler's response. The compression
 * middleware it installs only compresses when the content type it reads is a string, and
 * Next's node response adapter stores every header it copies from a Response as an array
 * (next/dist/server/base-http/node.js appendHeader), so the check fails and the body leaves
 * as identity.
 *
 * The upstream leg stays identity (see the proxy route's header comment): undici decodes a
 * compressed upstream body while content-length still describes the compressed bytes. So the
 * proxy compresses here, after the upstream body is known to be plain.
 */

const MIN_COMPRESS_BYTES = 1024;

// JSON (including +json types) and text. Never binary, never an event stream.
const COMPRESSIBLE_TYPE = /^(application\/(?:json|[\w.+-]+\+json)|text\/(?:plain|html|csv))\s*(;|$)/i;

/** True when the client lists gzip (or *) with a non-zero q value. */
export function acceptsGzip(acceptEncoding: string | null): boolean {
  if (!acceptEncoding) return false;
  return acceptEncoding.split(",").some((part) => {
    const [token, ...params] = part.trim().split(";");
    const name = token.trim().toLowerCase();
    if (name !== "gzip" && name !== "*") return false;
    const q = params.map((p) => p.trim()).find((p) => p.toLowerCase().startsWith("q="));
    return q === undefined || Number(q.slice(2)) > 0;
  });
}

/** Whether a response of this type is one the proxy would compress for a willing client. */
export function isCompressibleType(headers: Headers): boolean {
  return COMPRESSIBLE_TYPE.test(headers.get("content-type") || "");
}

/**
 * Whether to gzip this response for this request.
 *
 * Requires a known length of at least 1 KB, which also leaves every streamed body alone (a
 * download without content-length, an event stream): those are passed through untouched.
 */
export function shouldCompress(
  method: string,
  status: number,
  acceptEncoding: string | null,
  headers: Headers,
): boolean {
  if (method !== "GET" || status < 200 || status >= 300 || status === 204 || status === 206) {
    return false;
  }
  if (!acceptsGzip(acceptEncoding)) return false;
  if (headers.get("content-encoding")) return false;
  if (!isCompressibleType(headers)) return false;
  if (/attachment/i.test(headers.get("content-disposition") || "")) return false;
  if (/no-transform/i.test(headers.get("cache-control") || "")) return false;
  const length = Number(headers.get("content-length"));
  return Number.isFinite(length) && length >= MIN_COMPRESS_BYTES;
}

/** Add Accept-Encoding to Vary without duplicating it. */
export function varyOnAcceptEncoding(headers: Headers): void {
  const vary = headers.get("vary");
  if (!vary) {
    headers.set("vary", "Accept-Encoding");
    return;
  }
  const names = vary.split(",").map((v) => v.trim().toLowerCase());
  if (!names.includes("accept-encoding") && !names.includes("*")) {
    headers.set("vary", `${vary}, Accept-Encoding`);
  }
}

/**
 * Gzip `body` and fix the headers that describe it: no content-length (unknown until the
 * stream ends), content-encoding gzip, and a strong ETag weakened, since the bytes on the wire
 * differ from the bytes it named.
 */
export function gzipBody(
  body: ReadableStream<Uint8Array>,
  headers: Headers,
): ReadableStream<Uint8Array> {
  headers.delete("content-length");
  headers.set("content-encoding", "gzip");
  const etag = headers.get("etag");
  if (etag && !etag.startsWith("W/")) headers.set("etag", `W/${etag}`);
  return body.pipeThrough(new CompressionStream("gzip") as unknown as TransformStream<Uint8Array, Uint8Array>);
}
