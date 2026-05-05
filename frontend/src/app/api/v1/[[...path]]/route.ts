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

// Docker-internal backend URL (no browser access needed)
const BACKEND_URL =
  process.env.INTERNAL_BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:4050";

async function proxyRequest(req: NextRequest): Promise<Response> {
  const { pathname, search } = req.nextUrl;
  const targetUrl = `${BACKEND_URL}${pathname}${search}`;

  // Forward all headers except host (let the backend see its own host)
  const headers = new Headers(req.headers);
  headers.delete("host");
  // Tell backend not to compress — we handle the response directly
  headers.set("accept-encoding", "identity");

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body: req.body,
      // @ts-expect-error -- Node fetch supports duplex for streaming request bodies
      duplex: "half",
    });

    // Buffer the full (decompressed) body so Content-Length is accurate
    const body = await upstream.arrayBuffer();

    const responseHeaders = new Headers(upstream.headers);
    // Remove hop-by-hop and encoding headers that are no longer valid
    responseHeaders.delete("transfer-encoding");
    responseHeaders.delete("connection");
    responseHeaders.delete("content-encoding");
    // Set correct Content-Length for the decompressed body
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
