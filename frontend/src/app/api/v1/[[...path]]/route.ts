/**
 * Reverse-proxies /api/v1/* to the backend and streams the response back.
 *
 * Next.js rewrites redirect instead of proxying when the destination has a different
 * protocol or host, so this handler fetches server-side, avoiding CORS and mixed content.
 *
 * important: the backend runs GZipMiddleware, and Node's fetch() decodes gzip while
 * Content-Length/Content-Encoding still describe the compressed body, which truncates it in the
 * browser. buildForwardHeaders asks the backend for `identity` so the body streams through
 * unbuffered, and both headers are dropped below if it compresses anyway. The browser leg is
 * gzipped here (lib/proxy-compression.ts), because
 * Next's own compression never applies to a route handler's response. Because the body streams,
 * a backend failure mid-body reaches the client truncated instead of as the 502 JSON below.
 */
import { NextRequest, NextResponse } from "next/server";
import {
  AUTH_SIGNAL_HEADER,
  applyProxyAuth,
  finishProxyResponse,
  resolveProxyAuth,
} from "@/lib/api-proxy-auth";
import { inspectRequestTarget } from "@/lib/request-security";
import { editionAuth } from "@/lib/edition-auth";
import { sessionSecret } from "@/lib/session-cookie-echo";
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

// important: off unless a deployment opts in. The headers below are read off the inbound request
// and re-emitted as `x-orcastra-client-ip`, which the backend trusts first because this container
// sits inside the default trusted-proxy CIDRs. This port is also reachable from the internet,
// where the value is whatever the caller typed: an unauthenticated caller could choose the address
// the backend rate-limits on and writes to the audit trail, and rotate it to escape the per-IP
// bucket. A browser sends no X-Forwarded-For, so dashboard traffic loses nothing. Set
// FORWARD_CLIENT_IP_HEADERS=true only behind a trusted reverse proxy that sets these headers.
const FORWARD_CLIENT_IP_HEADERS = process.env.FORWARD_CLIENT_IP_HEADERS === "true";
const TRUSTED_CLIENT_IP_HEADERS = (
  process.env.TRUSTED_CLIENT_IP_HEADERS || "cf-connecting-ip,true-client-ip,cf-connecting-ipv6,x-forwarded-for,x-real-ip"
)
  .split(",")
  .map((headerName) => headerName.trim().toLowerCase())
  .filter(Boolean);

// Statuses that must never carry a response body (RFC 9110).
const NULL_BODY_STATUSES = new Set([204, 205, 304]);

// important: a deadline on response headers only. Some reads are slow to first byte (/regions
// cold is 15-45s, and lib/api.ts budgets 90s for it) and ISO upload chunks are slow to last
// byte, so an AbortSignal.timeout() covering the body would kill both. fetchWithTimeout in
// lib/api.ts owns the user-facing timeout; this backstop keeps a wedged backend from pinning a
// socket forever.
const UPSTREAM_HEADERS_TIMEOUT_MS = 100_000;
// important: a delete may hold its response until the cluster has finished. An instance delete
// waits up to 300 s for LXD (backend services/lxd/instance.py) and the client gives it 300 s
// (lib/api.ts vmAPI.delete); a shorter backstop would answer 502 while LXD goes on deleting.
// DELETE only: it carries no body, so a client trickling one cannot hold the longer window open
// (this route is reachable before authentication). 300 s is also undici's default.
const UPSTREAM_DELETE_HEADERS_TIMEOUT_MS = 300_000;

const HOP_BY_HOP_HEADERS = [
  "connection",
  "content-length",
  // Node answers a caller's 100-continue itself, and the backend fetch fails on the header. curl
  // sends it with any body over 1 MB.
  "expect",
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

  // An edition whose sign-in rides on the edge in front of this server adds what the backend
  // needs from it, after the rules above.
  editionAuth?.forwardHeaders?.(req, headers);

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

  // important: a browser request is authenticated by its session cookie here, and the session's
  // access token is attached for the backend (lib/api-proxy-auth.ts). A caller without the cookie
  // keeps its own Authorization header.
  const auth = await resolveProxyAuth(req, sessionSecret(), process.env.AUTH_URL ?? process.env.NEXTAUTH_URL);
  if (auth.kind === "refuse") {
    return NextResponse.json(
      { detail: auth.detail },
      {
        status: auth.status,
        headers: {
          "Cache-Control": "no-store",
          ...(auth.status === 401
            ? { [AUTH_SIGNAL_HEADER]: auth.signal }
            : { "X-Orcastra-Security-Action": auth.signal }),
        },
      }
    );
  }

  const targetUrl = buildTargetUrl(pathname, search);

  const headers = buildForwardHeaders(req);
  applyProxyAuth(headers, auth);

  const headersDeadline = new AbortController();
  const deadlineTimer = setTimeout(
    () => headersDeadline.abort(new Error("upstream headers timeout")),
    req.method === "DELETE" ? UPSTREAM_DELETE_HEADERS_TIMEOUT_MS : UPSTREAM_HEADERS_TIMEOUT_MS
  );

  // important: a streamed body cannot be replayed, so undici throws on a redirect for any method
  // that carries one and the catch below would call a healthy backend unavailable. Such a
  // redirect is answered as what it is; one without a body is still followed here, because its
  // Location names the internal backend host the browser cannot reach.
  const carriesBody = req.method !== "GET" && req.method !== "HEAD";

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body: req.body,
      redirect: carriesBody ? "manual" : "follow",
      // important: req.signal aborts when the browser disconnects. Without it the upstream
      // fetch and its backend connection outlive the client that asked for them, inside the
      // same single Node process that also renders every RSC payload.
      signal: AbortSignal.any([req.signal, headersDeadline.signal]),
      // @ts-expect-error -- Node fetch supports duplex for streaming request bodies
      duplex: "half",
    });

    // Headers are in. The body may legitimately take much longer, so stop the clock.
    clearTimeout(deadlineTimer);

    if (carriesBody && upstream.status >= 300 && upstream.status < 400) {
      console.error("[api-proxy] backend redirected a request with a body", {
        method: req.method,
        path: pathname,
        status: upstream.status,
      });
      await upstream.body?.cancel();
      return NextResponse.json(
        { error: "The backend redirected this request, which the proxy does not replay with its body" },
        { status: 502 }
      );
    }

    // 204/205/304 are null-body statuses: passing any body (even a zero-length
    // buffer) makes the Response constructor throw "Invalid response status
    // code", which the catch below would report as a false "Backend
    // unavailable" 502 for every no-content reply, ex; tus PATCH chunks.
    const isNullBody = NULL_BODY_STATUSES.has(upstream.status);
    // Stream the body straight through rather than buffering it into memory.
    const body = isNullBody ? null : upstream.body;

    const responseHeaders = new Headers(upstream.headers);
    // Hop-by-hop headers do not carry over to the browser leg.
    responseHeaders.delete("transfer-encoding");
    responseHeaders.delete("connection");
    finishProxyResponse(responseHeaders, auth);

    const upstreamEncoding = upstream.headers.get("content-encoding");
    if (isNullBody) {
      responseHeaders.delete("content-length");
    } else if (upstreamEncoding && upstreamEncoding.toLowerCase() !== "identity") {
      // important: identity was requested. If the backend compressed anyway, undici decodes
      // the stream while content-length still describes the compressed bytes, which truncates
      // the body in the browser. Drop both and let Node chunk it.
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

// Never cached or prerendered: every request goes to the backend.
export const dynamic = "force-dynamic";
