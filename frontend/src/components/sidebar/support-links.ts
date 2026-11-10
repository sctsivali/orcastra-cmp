// Where the sidebar sends a defect report and a question. Both are product destinations
// rather than per-deployment infrastructure, but they are overridable so an operator running
// a fork can point them at their own tracker and forum.
//
// important: the literal defaults below are also the build-time ENV values in
// frontend/Dockerfile and the patterns frontend/entrypoint.sh swaps at container start, so
// keep the three in step. The Dockerfile sets these variables before `npm run build`, so
// inside an image `process.env.X || DEFAULT` never reaches DEFAULT; the `||` serves a bare
// `npm run dev`.
const DEFAULT_BUG_REPORT_URL = "https://github.com/sctsivali/orcastra-cmp/issues/new";
const DEFAULT_COMMUNITY_URL = "https://discourse.orca.id";

export const BUG_REPORT_URL = process.env.NEXT_PUBLIC_BUG_REPORT_URL || DEFAULT_BUG_REPORT_URL;
export const COMMUNITY_URL = process.env.NEXT_PUBLIC_COMMUNITY_URL || DEFAULT_COMMUNITY_URL;

// The repository ships .github/ISSUE_TEMPLATE/bug_report.yml, which is a GitHub issue form.
// A form is prefilled through query parameters named after its own field ids, not through the
// ?body= parameter a plain markdown template accepts, so these two keys have to stay in step
// with that file. `version` and `environment` are ids in it today.
const ISSUE_FORM = "bug_report.yml";
const VERSION_FIELD = "version";
const ENVIRONMENT_FIELD = "environment";

// www included because a copied address often carries it. A GitHub Enterprise host is not
// matched: it may serve a different set of forms.
const GITHUB_HOSTS = new Set(["github.com", "www.github.com"]);

// Same build-time value the footer prints, from next.config.js `env`.
const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION || "dev";

// Prefills the issue form so a report arrives with the version already correct, which is the
// field a reporter is most likely to get wrong. Nothing identifying is added: no route, no
// account, no organization, no cluster name, no token.
export function buildBugReportUrl(environment?: string): string {
  try {
    const url = new URL(BUG_REPORT_URL);
    // A self-hosted tracker has no issue form and no fields to fill, so it is left untouched.
    if (!GITHUB_HOSTS.has(url.hostname)) return BUG_REPORT_URL;
    url.searchParams.set("template", ISSUE_FORM);
    url.searchParams.set(VERSION_FIELD, APP_VERSION);
    if (environment) url.searchParams.set(ENVIRONMENT_FIELD, environment);
    return url.toString();
  } catch {
    // important: `new URL` throws on a value with no scheme, and this runs inside the root
    // layout subtree, which has no error boundary above it. Returning the configured value
    // leaves one link broken rather than failing every route in the console.
    return BUG_REPORT_URL;
  }
}

// Client-only: navigator does not exist during the server render, so callers read this after
// mount. The user agent already reaches the tracker in the request headers when the link is
// opened, so putting it in the form adds no disclosure and saves the reporter typing it.
export function describeEnvironment(): string {
  if (typeof navigator === "undefined") return "";
  return navigator.userAgent;
}
