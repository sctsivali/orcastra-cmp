import { onAuthActorChanged } from "@/lib/api";

/** Unsaved text and the version of the file it was written against. */
export interface Draft {
  text: string;
  etag: string;
}

/** Kept past a dialog tab unmounting the editor. Memory only, never a disk. */
const drafts = new Map<string, Draft>();

export function draftKey(host: string, project: string, instance: string, path: string): string {
  return JSON.stringify([host, project, instance, path]);
}

export function readDraft(key: string): Draft | undefined {
  return drafts.get(key);
}

export function writeDraft(key: string, draft: Draft): void {
  drafts.set(key, draft);
}

export function clearDraft(key: string): void {
  drafts.delete(key);
}

export function hasDrafts(): boolean {
  return drafts.size > 0;
}

onAuthActorChanged(() => drafts.clear());
