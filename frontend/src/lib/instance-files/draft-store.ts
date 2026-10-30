import { onAuthActorChanged } from "@/lib/api";

/** Unsaved text, kept past a dialog tab unmounting the editor. Memory only, never a disk. */
const drafts = new Map<string, string>();

export function draftKey(host: string, project: string, instance: string, path: string): string {
  return JSON.stringify([host, project, instance, path]);
}

export function readDraft(key: string): string | undefined {
  return drafts.get(key);
}

export function writeDraft(key: string, text: string): void {
  drafts.set(key, text);
}

export function clearDraft(key: string): void {
  drafts.delete(key);
}

export function hasDrafts(): boolean {
  return drafts.size > 0;
}

onAuthActorChanged(() => drafts.clear());
