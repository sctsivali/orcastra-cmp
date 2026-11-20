// Relevance for the command palette. Deterministic tiers rather than fuzzy matching: operators
// type near-exact resource names, and a loose match in a console that can stop or delete an
// instance sends them to the wrong one.

const EXACT = 100;
const PREFIX = 80;
const WORD_START = 65;
const SUBSTRING = 50;

/** Resource groups wait for this many characters; one matches nearly everything. */
export const MIN_RESOURCE_QUERY = 2;

export type Field = readonly [value: string | null | undefined, weight: number];

function startsAWord(value: string, query: string): boolean {
  let at = value.indexOf(query);
  while (at !== -1) {
    if (at === 0 || !/[a-z0-9]/i.test(value[at - 1])) return true;
    at = value.indexOf(query, at + 1);
  }
  return false;
}

export function scoreField(query: string, value: string | null | undefined, weight = 1): number {
  const q = query.trim().toLowerCase();
  const v = (value ?? "").trim().toLowerCase();
  if (!q || !v) return 0;
  let base = 0;
  if (v === q) base = EXACT;
  else if (v.startsWith(q)) base = PREFIX;
  else if (startsAWord(v, q)) base = WORD_START;
  else if (v.includes(q)) base = SUBSTRING;
  return Math.round(base * weight);
}

export interface Ranked<T> {
  items: T[];
  total: number;
}

/** The best-scoring rows first, cut to `limit`; `total` counts every match. */
export function rank<T>(rows: readonly T[], query: string, fields: (row: T) => readonly Field[], limit: number): Ranked<T> {
  const scored: Array<{ row: T; score: number }> = [];
  for (const row of rows) {
    let best = 0;
    for (const [value, weight] of fields(row)) best = Math.max(best, scoreField(query, value, weight));
    if (best > 0) scored.push({ row, score: best });
  }
  scored.sort((a, b) => b.score - a.score);
  return { items: scored.slice(0, limit).map((s) => s.row), total: scored.length };
}
