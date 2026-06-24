// Global search response types (mirror backend app/models/search_schemas.py).

export interface SearchResultItem {
  type: string;
  id: string;
  title: string;
  subtitle: string;
  score: number;
  match_field: string | null;
  href: string;
  meta: Record<string, unknown>;
}

export interface SearchGroup {
  type: string;
  label: string;
  total: number;
  items: SearchResultItem[];
}

export interface SearchResponse {
  query: string;
  took_ms: number;
  degraded: boolean;
  total: number;
  groups: SearchGroup[];
}
