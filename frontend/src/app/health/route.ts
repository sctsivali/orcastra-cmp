/** Readiness proxy: forwards /health to the backend. */
import { NextResponse } from "next/server";

const BACKEND_URL =
  process.env.INTERNAL_BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:4050";

// The backend's own healthcheck uses a 3s timeout, so waiting longer than that here
// would only turn a wedged backend into a wedged probe.
const HEALTH_TIMEOUT_MS = 3_000;

export async function GET(): Promise<Response> {
  try {
    const upstream = await fetch(`${BACKEND_URL}/health`, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    const data = await upstream.json();
    return NextResponse.json(data, { status: upstream.status });
  } catch {
    return NextResponse.json({ error: "Backend unavailable" }, { status: 502 });
  }
}
