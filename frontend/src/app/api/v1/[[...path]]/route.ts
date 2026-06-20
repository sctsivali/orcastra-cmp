/**
 * API Proxy Route — reverse-proxies /api/v1/* to the backend server.
 *
 * Next.js rewrites redirect (not proxy) when the destination has a different
 * protocol/host. This route handler does a proper server-side fetch and streams
 * the response back to the browser, avoiding CORS and mixed-content issues.
 *
 * IMPORTANT: The backend uses GZipMiddleware. Node.js fetch() auto-decompresses
 * gzip responses, but the original Content-Length/Content-Encoding headers still
 * reflect the compressed payload. We must buffer the response and set correct
 * headers to prevent the browser from truncating the decompressed body.
 */
import { NextRequest, NextResponse } from "next/server";
import { inspectRequestTarget } from "@/lib/request-security";

// Docker-internal backend URL (no browser access needed)
const BACKEND_URL =
  process.env.INTERNAL_BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:4050";

const FORWARD_CLIENT_IP_HEADERS = process.env.FORWARD_CLIENT_IP_HEADERS !== "false";
const TRUSTED_CLIENT_IP_HEADERS = (
  process.env.TRUSTED_CLIENT_IP_HEADERS || "cf-connecting-ip,true-client-ip,cf-connecting-ipv6,x-forwarded-for,x-real-ip"
)
  .split(",")
  .map((headerName) => headerName.trim().toLowerCase())
  .filter(Boolean);

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

  // SECURITY: never let a browser inject client-cert auth headers through the generic
  // proxy. The only legitimate carrier of these is the NextAuth cert-login flow, which
  // calls the backend directly (server-side) with the proxy secret.
  headers.delete("x-auth-proxy-secret");
  for (const name of Array.from(headers.keys())) {
    if (name.toLowerCase().startsWith("x-ssl-client-")) headers.delete(name);
  }

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

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body: req.body,
      // @ts-expect-error -- Node fetch supports duplex for streaming request bodies
      duplex: "half",
    });

    const responseHeaders = new Headers(upstream.headers);
    // Remove hop-by-hop and encoding headers that are no longer valid
    responseHeaders.delete("transfer-encoding");
    responseHeaders.delete("connection");
    responseHeaders.delete("content-encoding");

    // Null-body statuses (204 No Content, 304 Not Modified, 1xx) must not carry a body
    // or Content-Length - constructing a Response with one throws and surfaces as a 502.
    const nullBody = upstream.status === 204 || upstream.status === 304 || upstream.status < 200;
    if (nullBody) {
      responseHeaders.delete("content-length");
      return new Response(null, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: responseHeaders,
      });
    }

    // Buffer the full (decompressed) body so Content-Length is accurate
    const body = await upstream.arrayBuffer();
    responseHeaders.set("content-length", String(body.byteLength));

    return new Response(body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch {
    return NextResponse.json(
      { error: "Backend unavailable" },
      { status: 502 }
    );
  }
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;

// Allow streaming request bodies and large payloads
export const dynamic = "force-dynamic";
