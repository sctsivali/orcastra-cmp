import { describeReadError } from "@/lib/read-error";

export type PickerNoticeKind = "loading" | "error" | "rate-limited" | "unreachable";

export interface PickerNotice {
  kind: PickerNoticeKind;
  text: string;
  /** Set for a rate-limited read; the notice counts it down before offering Retry. */
  retryAfterSeconds?: number;
}

export interface PickerState {
  disabled: boolean;
  placeholder: string;
  /** What the list says when a search matches nothing, or the read returned nothing. */
  emptyText: string;
  notice?: PickerNotice;
}

export interface PickerStateInput {
  /** The control waits on another choice (ex; a node). */
  gated?: boolean;
  gatedText?: string;
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  optionCount: number;
  /** Plural noun in sentence case, ex; "Storage pools". */
  subject: string;
  /** Where the read went, ex; the node's display name. */
  nodeLabel?: string;
  selectText?: string;
  emptyText?: string;
}

/**
 * The four states every data-driven picker has to tell apart: waiting on another choice,
 * reading, failed (with why and a retry), and answered. One function decides them so a pool
 * picker and a network picker cannot drift, and so "empty" is reserved for a read that
 * answered with nothing rather than one that never answered.
 */
export function derivePickerState(input: PickerStateInput): PickerState {
  const subjectLower = input.subject.toLowerCase();
  const where = input.nodeLabel ? ` on ${input.nodeLabel}` : "";
  const emptyText = input.emptyText ?? `No ${subjectLower}${where}.`;

  if (input.gated) {
    return { disabled: true, placeholder: input.gatedText ?? "Select a node first", emptyText };
  }
  if (input.isLoading) {
    return {
      disabled: true,
      placeholder: `Loading ${subjectLower}`,
      emptyText,
      notice: { kind: "loading", text: `Reading ${subjectLower}${where}.` },
    };
  }
  if (input.isError) {
    const read = describeReadError(input.error);
    const kind: PickerNoticeKind =
      read.kind === "rate-limited" ? "rate-limited" : read.kind === "unreachable" ? "unreachable" : "error";
    return {
      disabled: true,
      placeholder: `${input.subject} could not be read`,
      emptyText,
      notice: {
        kind,
        text: `${input.subject} could not be read${where}: ${read.reason}.`,
        retryAfterSeconds: read.retryAfterSeconds,
      },
    };
  }
  if (input.optionCount === 0) {
    return { disabled: true, placeholder: emptyText, emptyText };
  }
  return { disabled: false, placeholder: input.selectText ?? `Select ${singular(subjectLower)}`, emptyText };
}

function singular(plural: string): string {
  if (plural.endsWith("ies")) return `${plural.slice(0, -3)}y`;
  if (plural.endsWith("s")) return plural.slice(0, -1);
  return plural;
}
