import {
  AlertTriangle,
  Ban,
  Clock,
  Lock,
  SearchX,
  ServerCrash,
  ServerOff,
  WifiOff,
  type LucideIcon,
} from "lucide-react";
import { APIError } from "@/types/api";

/**
 * Why a cluster member's resources could not be read, in words an operator can act on.
 *
 * A cluster that is down, a slow cluster, a member that is gone and a missing permission each
 * get their own message. There is no "member reported nothing" case: the endpoint answers a
 * fully populated body or an error.
 *
 * Titles are literals rather than templates, so they stay greppable; names belong in the
 * description. The static heading guard reads `title` only as a JSX prop, so it does not check
 * these object keys.
 */
export interface MemberResourceFailure {
  icon: LucideIcon;
  title: string;
  description: string;
  /** Whether asking again could plausibly change the answer. */
  retryable: boolean;
  /** The member is not there to read, so closing is the useful action. */
  gone: boolean;
}

export function memberResourceFailure(
  error: unknown,
  ctx: { node: string; member: string },
): MemberResourceFailure {
  const node = ctx.node || "the cluster";
  const member = ctx.member || "this member";
  const status = error instanceof APIError ? error.status : null;

  // The cluster's own verdict on the member ("Member 'mc3' is Offline: No heartbeat for 27s")
  // also arrives as a 503, but the cluster did answer: only the member is down, so the
  // cluster's own words are shown.
  const detail = error instanceof APIError && typeof error.detail === "string" ? error.detail : "";
  if (status === 503 && /^Member '.+' is \S+/.test(detail)) {
    return {
      icon: ServerOff,
      title: "This member could not be read",
      description: detail,
      retryable: true,
      gone: false,
    };
  }

  switch (status) {
    case 503:
      return {
        icon: ServerOff,
        title: "Could not reach this cluster",
        description: `${node} did not answer, so its resources could not be read. The dashboard backs off for a minute after a failure, so a retry may take a moment to go through.`,
        retryable: true,
        gone: false,
      };
    case 504:
      return {
        icon: Clock,
        title: "The cluster did not answer in time",
        description: `${node} accepted the request but did not finish it. That usually means the cluster is busy rather than unreachable, so retrying often works.`,
        retryable: true,
        gone: false,
      };
    case 502:
      return {
        icon: AlertTriangle,
        title: "The cluster refused this request",
        description: `${node} answered, but would not report resources for ${member}. Its own log will say why.`,
        retryable: true,
        gone: false,
      };
    case 404:
      return {
        icon: SearchX,
        title: "Not a member of this cluster",
        description: `${node} does not list ${member}. The cluster may have changed since this panel was opened.`,
        retryable: false,
        gone: true,
      };
    case 403:
      return {
        icon: Lock,
        title: "Not authorized to read this cluster",
        description: `Your organization does not have access to ${node}.`,
        retryable: false,
        gone: false,
      };
    case 401:
      return {
        icon: Lock,
        title: "Your session has expired",
        description: "Sign in again to read this member.",
        retryable: false,
        gone: false,
      };
    case 429:
      return {
        icon: Ban,
        title: "Too many requests right now",
        description:
          "The dashboard is being rate limited. It will try again on its own in a moment.",
        retryable: false,
        gone: false,
      };
    // The client mints status 0 when the browser could not reach the dashboard at all, which
    // is a different problem from the cluster being down and has a different remedy.
    case 0:
      return {
        icon: WifiOff,
        title: "Could not reach the dashboard",
        description:
          "The browser could not reach the dashboard. Check your connection and try again.",
        retryable: true,
        gone: false,
      };
    default:
      return {
        icon: ServerCrash,
        title: "Could not read this member's resources",
        description: `The dashboard hit a fault while reading ${node}. This one is ours rather than the cluster's.`,
        retryable: true,
        gone: false,
      };
  }
}
