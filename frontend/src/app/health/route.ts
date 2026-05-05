/**
 * Health check proxy — forwards /health to the backend.
 */
import { NextResponse } from "next/server";

const BACKEND_URL =
  process.env.INTERNAL_BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:4050";

export async function GET(): Promise<Response> {
  try {
    const upstream = await fetch(`${BACKEND_URL}/health`);
    const data = await upstream.json();
    return NextResponse.json(data, { status: upstream.status });
  } catch {
    return NextResponse.json({ error: "Backend unavailable" }, { status: 502 });
  }
}
