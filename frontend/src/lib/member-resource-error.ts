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
 * The modal used to render one hand-rolled block, "No resource data / Unable to fetch resource
 * information for this member", for every one of these. A cluster that is down, a cluster that
 * is merely slow, a member that no longer exists and an account that may not see the cluster
 * all produced the same sentence, so the only way to tell an outage from a permission problem
 * was to open the network tab.
 *
 * There is no genuine "this member reported nothing" case to model. The endpoint always
 * answers a fully populated body or raises, so every empty render was an error being
 * mislabelled.
 *
 * note: titles are literals rather than templates. They stay greppable, and the static heading
 * guard reads `title` as a JSX prop rather than as an object key, so the browser pass is what
 * covers them. Names belong in the description, where they are not part of the vocabulary.
 */
export interface MemberResourceFailure {
  icon: LucideIcon;
  title: string;
  description: string;
  /** Whether asking again could plausibly change the answer. */
  retryable: boolean;
  /** The member is not there to read, so the useful action is to close, not to retry. */
  gone: boolean;
}

export function memberResourceFailure(
  error: unknown,
  ctx: { node: string; member: string },
): MemberResourceFailure {
  const node = ctx.node || "the cluster";
  const member = ctx.member || "this member";
  const status = error instanceof APIError ? error.status : null;

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
