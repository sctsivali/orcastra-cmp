import { fetchRead, type ReadOptions } from "@/lib/api";
import type { KernelAdvisoryResponse } from "@/types/kernel-advisory";

// A forced read asks every member of every cluster in scope, up to 30 s per cluster on the
// server, so it gets the extended budget the other fan-out reads use.
const FRESH_TIMEOUT_MS = 90_000;

export const kernelAdvisoryAPI = {
  list: (opts: ReadOptions = {}) =>
    fetchRead<KernelAdvisoryResponse>(
      "/security/kernel-advisories",
      opts,
      opts.fresh ? FRESH_TIMEOUT_MS : undefined,
    ),
};
