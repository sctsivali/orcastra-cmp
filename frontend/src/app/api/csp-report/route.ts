/**
 * Where browsers send Content Security Policy violation reports (lib/csp.ts `report-uri`).
 *
 * Unauthenticated by nature: a browser sends no credentials with a report. Each accepted violation
 * becomes one structured log line (log_type csp_report) that reaches the deployment's logs; the
 * body is capped at 8 KB, one process-wide budget bounds what a flood can log, and every field is
 * sanitized (lib/csp-report.ts). The answer is always empty, so a sender learns nothing.
 */
import { NextRequest } from "next/server";
import { CSP_REPORT_TYPES, MAX_REPORT_BYTES, parseCspReport, readCapped, tokenBucket } from "@/lib/csp-report";

// 30 at once, then one every two seconds, across every sender.
const budget = tokenBucket(30, 0.5);

const EMPTY = () => new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });

export async function POST(request: NextRequest): Promise<Response> {
  const contentType = request.headers.get("content-type") ?? "";
  if (!CSP_REPORT_TYPES.some((type) => contentType.toLowerCase().startsWith(type))) {
    return new Response(null, { status: 415 });
  }
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_REPORT_BYTES) return new Response(null, { status: 413 });
  const text = await readCapped(request.body);
  if (text === null) return new Response(null, { status: 413 });
  for (const violation of parseCspReport(contentType, text)) {
    if (!budget.take()) break;
    console.warn(JSON.stringify({ log_type: "csp_report", ...violation }));
  }
  return EMPTY();
}

export const dynamic = "force-dynamic";
