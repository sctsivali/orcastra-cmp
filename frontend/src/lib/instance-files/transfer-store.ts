import { create } from "zustand";
import { onAuthActorChanged } from "@/lib/api";
import type { GuestTarget, OnConflict } from "./api";

export type TransferState =
  | "queued" | "uploading" | "finishing" | "done" | "failed" | "cancelled" | "conflict" | "interrupted";

export interface Transfer {
  id: string;
  target: GuestTarget;
  /** The name it lands under, which Keep Both can change. */
  name: string;
  /** The name it had on the person's disk, which is what a resume has to match. */
  sourceName?: string;
  dest: string;
  size: number;
  sent: number;
  state: TransferState;
  onConflict: OnConflict;
  error?: string;
  errorCode?: string;
  startedAt?: number;
  /** Bytes per second over the last few chunks. */
  rate: number;
  batch: string;
  /** Upload session, kept so a retry continues rather than starts again. */
  token?: string;
}

interface TransferStore {
  transfers: Transfer[];
  add: (items: Transfer[]) => void;
  patch: (id: string, change: Partial<Transfer>) => void;
  remove: (ids: string[]) => void;
  reset: () => void;
}

export const useTransferStore = create<TransferStore>((set) => ({
  transfers: [],
  add: (items) => set((state) => ({ transfers: [...state.transfers, ...items] })),
  patch: (id, change) =>
    set((state) => ({
      transfers: state.transfers.map((item) => (item.id === id ? { ...item, ...change } : item)),
    })),
  remove: (ids) => set((state) => ({ transfers: state.transfers.filter((item) => !ids.includes(item.id)) })),
  reset: () => set({ transfers: [] }),
}));

const ACTIVE: readonly TransferState[] = ["queued", "uploading", "finishing"];

export function isSameTarget(a: GuestTarget, b: GuestTarget): boolean {
  return a.host === b.host && a.project === b.project && a.instanceName === b.instanceName;
}

export function isActive(transfer: Transfer): boolean {
  return ACTIVE.includes(transfer.state);
}

export function hasActiveTransfers(): boolean {
  return useTransferStore.getState().transfers.some(isActive);
}

/** The File handles, beside the store: they are not state a render should compare or persist. */
export const transferFiles = new Map<string, File>();

// Actor changes are synchronous auth boundaries: B must not inherit A's queue, tokens or names.
const actorListeners = new Set<() => void>();

export function onTransfersReset(listener: () => void): () => void {
  actorListeners.add(listener);
  return () => actorListeners.delete(listener);
}

onAuthActorChanged(() => {
  actorListeners.forEach((listener) => listener());
  transferFiles.clear();
  useTransferStore.getState().reset();
});
