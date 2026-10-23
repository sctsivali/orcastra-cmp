/**
 * API Proxy Route: reverse-proxies /api/v1/* to the backend server.
 *
 * Next.js rewrites redirect (not proxy) when the destination has a different
 * protocol/host. This route handler does a proper server-side fetch and streams
 * the response back to the browser, avoiding CORS and mixed-content issues.
 *
 * IMPORTANT: The backend uses GZipMiddleware. Node.js fetch() auto-decompresses gzip
 * responses, but the original Content-Length/Content-Encoding headers still reflect the
 * compressed payload, which truncates the body in the browser. buildForwardHeaders asks the
 * backend for `identity` precisely so that cannot happen, which is what makes it safe to
 * stream the body through instead of buffering it. If the backend ever compresses anyway we
 * drop both headers below and let Node chunk the response. The browser leg is gzipped here
 * instead (lib/proxy-compression.ts), because Next's own compression never applies to a
 * route handler's response.
 *
 * note: because the body streams, an upstream failure that happens mid-body reaches the
 * client as a truncated response rather than the 502 JSON below, whose headers have already
 * been sent. That is how every reverse proxy behaves and only occurs if the backend dies
 * part-way through a response.
 */
import { NextRequest, NextResponse } from "next/server";
import { inspectRequestTarget } from "@/lib/request-security";
import {
  gzipBody,
  isCompressibleType,
  shouldCompress,
  varyOnAcceptEncoding,
} from "@/lib/proxy-compression";

// Docker-internal backend URL (no browser access needed)
const BACKEND_URL =
  process.env.INTERNAL_BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:4050";

// important: OFF unless a deployment opts in, and it used to be the other way round.
//
// The headers below are read off the INBOUND request and re-emitted as `x-orcastra-client-ip`,
// which the backend treats as the highest-priority truth because this container sits inside the
// default trusted-proxy CIDRs. That is correct when a real reverse proxy sets them. This port is
// also reachable straight off the internet, and there the value is whatever the caller typed, so
// an unauthenticated caller could choose the address the backend rate-limits on and writes to the
// audit trail, and rotate it to make the per-IP bucket bound nothing.
//
// Turning it off costs legitimate traffic nothing: a browser does not send X-Forwarded-For, so
// dashboard requests already resolve to this container either way. Only a deployment that
// genuinely sits behind a trusted proxy should set FORWARD_CLIENT_IP_HEADERS=true, and there the
// header means what it says.
const FORWARD_CLIENT_IP_HEADERS = process.env.FORWARD_CLIENT_IP_HEADERS === "true";
const TRUSTED_CLIENT_IP_HEADERS = (
  process.env.TRUSTED_CLIENT_IP_HEADERS || "cf-connecting-ip,true-client-ip,cf-connecting-ipv6,x-forwarded-for,x-real-ip"
)
  .split(",")
  .map((headerName) => headerName.trim().toLowerCase())
  .filter(Boolean);

// Statuses that must never carry a response body (RFC 9110).
const NULL_BODY_STATUSES = new Set([204, 205, 304]);

// important: a deadline on response HEADERS, not on the whole transaction. Some reads are
// legitimately slow to first byte (/regions cold is 15-45s, and lib/api.ts budgets 90s for
// it) and ISO upload chunks are legitimately slow to last byte, so an AbortSignal.timeout()
// covering the body would kill both. The user-facing timeout stays owned by fetchWithTimeout
// in lib/api.ts; this is only a backstop so a wedged backend cannot pin a socket forever.
const UPSTREAM_HEADERS_TIMEOUT_MS = 100_000;
// important: a write may legitimately hold its response until the cluster has finished. An
// instance delete waits up to 300 s for LXD (backend services/lxd/instance.py) and the client
// gives it 300 s (lib/api.ts vmAPI.delete). A 100 s backstop turned a slow delete into a 502:
// the row came back on screen while LXD went on deleting it. 300 s is also undici's own
// default headers timeout, so nothing longer would take effect.
const UPSTREAM_WRITE_HEADERS_TIMEOUT_MS = 300_000;

const HOP_BY_HOP_HEADERS = [
  "connection",
  "content-length",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

function isSupportedBackendUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function buildTargetUrl(pathname: string, search: string): string {
  const targetUrl = new URL(BACKEND_URL);
  targetUrl.pathname = pathname;
  targetUrl.search = search;
  return targetUrl.toString();
}

function isPrivateIpv4(candidate: string): boolean {
  const parts = candidate.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return true;
  }

  const [first, second] = parts;
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    first >= 224 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168)
  );
}

function isPrivateIpv6(candidate: string): boolean {
  const normalized = candidate.toLowerCase();
  return (
    normalized === "::1" ||
    normalized === "::" ||
    normalized.startsWith("fc") ||
    normalized.startsWith("fd") ||
    normalized.startsWith("fe80:")
  );
}

function firstIpHeaderValue(value: string | null): string | null {
  if (!value) return null;
  return value.split(",")[0]?.trim() || null;
}

function isValidPublicIpHeader(value: string | null): value is string {
  const candidate = firstIpHeaderValue(value);
  if (!candidate || candidate.length > 45) return false;
  return /^[0-9a-f:.]+$/i.test(candidate);
}

function trustedForwardedClientIp(req: NextRequest): { ip: string; source: string } | null {
  for (const headerName of TRUSTED_CLIENT_IP_HEADERS) {
    const candidate = firstIpHeaderValue(req.headers.get(headerName));
    if (!isValidPublicIpHeader(candidate)) continue;
    if (candidate.includes(".") && candidate.includes(":")) continue;
    if (candidate.includes(":")) {
      if (isPrivateIpv6(candidate)) continue;
    } else if (isPrivateIpv4(candidate)) {
      continue;
    }
    return { ip: candidate, source: headerName };
  }

  return null;
}

function buildForwardHeaders(req: NextRequest): Headers {
  const headers = new Headers(req.headers);

  HOP_BY_HOP_HEADERS.forEach((headerName) => headers.delete(headerName));
  headers.delete("host");
  headers.delete("cf-connecting-ip");
  headers.delete("true-client-ip");
  headers.delete("x-real-ip");
  headers.delete("x-forwarded-for");
  headers.delete("forwarded");
  headers.delete("x-orcastra-client-ip");
  headers.delete("x-orcastra-client-ip-source");

  if (FORWARD_CLIENT_IP_HEADERS) {
    const forwardedClientIp = trustedForwardedClientIp(req);

    if (forwardedClientIp) {
      headers.set("x-orcastra-client-ip", forwardedClientIp.ip);
      headers.set("x-orcastra-client-ip-source", forwardedClientIp.source);
      headers.set("x-forwarded-for", forwardedClientIp.ip);
      headers.set("x-real-ip", forwardedClientIp.ip);
    }
  }

  headers.set("x-forwarded-host", req.headers.get("host") || req.nextUrl.host);
  headers.set("x-forwarded-proto", req.nextUrl.protocol.replace(":", ""));
  headers.set("accept-encoding", "identity");

  return headers;
}

async function proxyRequest(req: NextRequest): Promise<Response> {
  const { pathname, search } = req.nextUrl;
  const decision = inspectRequestTarget(req.method, pathname, search);

  if (decision) {
    return NextResponse.json(
      { detail: decision.statusCode === 404 ? "Not found" : "Method not allowed" },
      {
        status: decision.statusCode,
        headers: { "X-Orcastra-Security-Action": decision.reason },
      }
    );
  }

  if (!isSupportedBackendUrl(BACKEND_URL)) {
    return NextResponse.json({ error: "Invalid backend URL" }, { status: 502 });
  }

  const targetUrl = buildTargetUrl(pathname, search);

  const headers = buildForwardHeaders(req);

  const headersDeadline = new AbortController();
  const deadlineTimer = setTimeout(
    () => headersDeadline.abort(new Error("upstream headers timeout")),
    req.method === "GET" || req.method === "HEAD"
      ? UPSTREAM_HEADERS_TIMEOUT_MS
      : UPSTREAM_WRITE_HEADERS_TIMEOUT_MS
  );

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body: req.body,
      // important: req.signal aborts when the browser disconnects. Without it the upstream
      // fetch and its backend connection outlive the client that asked for them, inside the
      // same single Node process that also renders every RSC payload.
      signal: AbortSignal.any([req.signal, headersDeadline.signal]),
      // @ts-expect-error -- Node fetch supports duplex for streaming request bodies
      duplex: "half",
    });

    // Headers are in. The body may legitimately take much longer, so stop the clock.
    clearTimeout(deadlineTimer);

    // 204/205/304 are null-body statuses: passing ANY body (even a zero-length
    // buffer) makes the Response constructor throw "Invalid response status
    // code", which the catch below would report as a bogus "Backend
    // unavailable" 502. This hit every no-content reply, e.g. tus PATCH chunks.
    const isNullBody = NULL_BODY_STATUSES.has(upstream.status);
    // Stream the body straight through rather than buffering it into memory.
    const body = isNullBody ? null : upstream.body;

    const responseHeaders = new Headers(upstream.headers);
    // Remove hop-by-hop headers that are no longer valid
    responseHeaders.delete("transfer-encoding");
    responseHeaders.delete("connection");

    const upstreamEncoding = upstream.headers.get("content-encoding");
    if (isNullBody) {
      responseHeaders.delete("content-length");
    } else if (upstreamEncoding && upstreamEncoding.toLowerCase() !== "identity") {
      // important: we asked for identity. If the backend compressed anyway, undici decodes
      // the stream while content-length still describes the compressed bytes, which is the
      // exact truncation this route was written to avoid. Drop both and let Node chunk it.
      responseHeaders.delete("content-encoding");
      responseHeaders.delete("content-length");
    }
    // else: identity confirmed, so the upstream content-length is accurate. Keeping it is
    // what gives the browser a real progress bar on a download that sends one.

    // The browser leg is compressed here, because Next's own compression never reaches a route
    // handler (lib/proxy-compression.ts says why). Only JSON and text with a known length of at
    // least 1 KB, only for a client that accepts gzip: downloads, uploads and streams pass through.
    let outgoing: ReadableStream<Uint8Array> | null = body;
    if (!isNullBody && isCompressibleType(responseHeaders)) {
      varyOnAcceptEncoding(responseHeaders);
      if (
        upstream.body &&
        shouldCompress(req.method, upstream.status, req.headers.get("accept-encoding"), responseHeaders)
      ) {
        outgoing = gzipBody(upstream.body, responseHeaders);
      }
    }

    return new Response(outgoing, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch (err) {
    console.error("[api-proxy] upstream fetch failed", {
      method: req.method,
      path: pathname,
      error: err instanceof Error ? `${err.name}: ${err.message}` : String(err),
    });
    return NextResponse.json(
      { error: "Backend unavailable" },
      { status: 502 }
    );
  } finally {
    clearTimeout(deadlineTimer);
  }
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;

// Allow streaming request bodies and large payloads
export const dynamic = "force-dynamic";
