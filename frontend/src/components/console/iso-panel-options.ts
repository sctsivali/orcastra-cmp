export interface ISOInfo {
  name: string;
  pool: string;
  volume: string;
  path: string;
  size: number;
  createdAt?: string;
}

// LXD gives higher boot.priority values precedence over the root disk.
export const BOOT_PRIORITY_OPTIONS = [
  { value: 10, label: "10 - Boot first (install from ISO)" },
  { value: 5, label: "5 - High" },
  { value: 1, label: "1 - Above an unset disk" },
  { value: 0, label: "0 - Lowest (boot from disk first)" },
];
export const DEFAULT_BOOT_PRIORITY = 10;
