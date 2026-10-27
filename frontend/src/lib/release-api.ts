import { fetchAPI, fetchRead, type ReadOptions } from "@/lib/api";
import type { ReleaseStatus } from "@/types/release";

export const releaseAPI = {
  /** The last read of the release manifest; never reaches the internet itself. */
  get: (opts: ReadOptions = {}) => fetchRead<ReleaseStatus>("/system/release-status", opts),
  /** Read the manifest now. At most once a minute across the deployment (429 otherwise). */
  checkNow: () => fetchAPI<ReleaseStatus>("/system/release-status/check", { method: "POST" }),
};
