/**
 * Where browsers send Content Security Policy violation reports (lib/csp.ts `report-uri`).
 *
 * Unauthenticated by nature: a browser sends no credentials with a report. Each accepted violation
 * becomes one structured log line (log_type csp_report) that reaches the deployment's logs; the
 * body is capped at 8 KB, reports about another site's pages are dropped, one process-wide budget
 * per disposition bounds what a flood can log (drops are summarized as csp_report_dropped), and
 * every field is sanitized (lib/csp-report.ts). The answer is always empty, so a sender learns
 * nothing. A csp_report line is written by whoever sent it: evidence to look at, not proof.
 */
import { NextRequest } from "next/server";
import {
  CSP_REPORT_TYPES,
  MAX_REPORT_BYTES,
  fromThisDeployment,
  parseCspReport,
  readCapped,
  tokenBucket,
} from "@/lib/csp-report";

// Per disposition, 30 at once then one every two seconds, across every sender: a flood of
// report-only noise cannot spend the budget an enforced violation (a blocked script) needs.
const budgets = { enforce: tokenBucket(30, 0.5), report: tokenBucket(30, 0.5) };
const SUMMARY_EVERY_MS = 60_000;
let dropped = 0;
let lastSummary = 0;

const EMPTY = () => new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });

/** How many reports the budgets dropped, at most once a minute, so a flood is visible. */
function summarizeDrops(now: number): void {
  if (dropped === 0 || now - lastSummary < SUMMARY_EVERY_MS) return;
  console.warn(JSON.stringify({ log_type: "csp_report_dropped", count: dropped }));
  dropped = 0;
  lastSummary = now;
}

export async function POST(request: NextRequest): Promise<Response> {
  const contentType = request.headers.get("content-type") ?? "";
  if (!CSP_REPORT_TYPES.some((type) => contentType.toLowerCase().startsWith(type))) {
    return new Response(null, { status: 415 });
  }
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_REPORT_BYTES) return new Response(null, { status: 413 });
  const text = await readCapped(request.body);
  if (text === null) return new Response(null, { status: 413 });
  const configuredUrl = process.env.AUTH_URL ?? process.env.NEXTAUTH_URL;
  for (const violation of parseCspReport(contentType, text)) {
    // Another site's report-uri pointed here: neither its noise nor its budget is ours.
    if (!fromThisDeployment(violation, request.headers, configuredUrl)) continue;
    const budget = violation.disposition === "enforce" ? budgets.enforce : budgets.report;
    if (!budget.take()) {
      dropped += 1;
      continue;
    }
    console.warn(JSON.stringify({ log_type: "csp_report", ...violation }));
  }
  summarizeDrops(Date.now());
  return EMPTY();
}

export const dynamic = "force-dynamic";
