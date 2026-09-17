/**
 * Liveness: is this Node process serving requests. Nothing else.
 *
 * important: this is the Docker healthcheck target (docker-compose.prod.yml, frontend/Dockerfile).
 * It must depend on nothing outside the process: no backend call (that is /health, the readiness
 * proxy), no auth (the middleware matcher exempts it), no redirect. The previous probe asked `/`,
 * an auth-gated page whose redirect pointed at the public origin, so the container's health was
 * judged by the edge in front of it and autoheal restarted a working frontend on every edge 502.
 */
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const STARTED_AT = Date.now();

export function GET(): Response {
  return NextResponse.json(
    {
      status: "alive",
      service: "orcastra-dashboard-frontend",
      version: process.env.NEXT_PUBLIC_APP_VERSION || "dev",
      uptime_s: Math.round((Date.now() - STARTED_AT) / 1000),
    },
    { headers: { "Cache-Control": "no-store" } }
  );
}
