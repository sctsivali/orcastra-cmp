import type { PersonalizationSettings } from "@/types/personalization";

/** Which layer is on top of what an account sees (backend services/branding/resolve.py). */
export type BrandingSource = "organization" | "provider" | "deployment" | "default";

/** GET /organizations/my-preferences: what this account sees, resolved on the server. */
export interface MyBranding {
  /** 0 when the account sees the deployment default. */
  organization_id: number;
  settings: Partial<PersonalizationSettings>;
  updated_by: string | null;
  updated_at: string | null;
  version: string;
  source: BrandingSource;
  subject: "organization" | "deployment";
}

export interface MyBrandingVersion {
  organization_id: number;
  updated_at: string | null;
  version: string;
}

export interface BrandingProviderRef {
  id: number;
  name: string;
  link_source: "provision" | "migration" | "admin" | null;
}

/** GET /organizations/{id}/preferences: the organization's own row and what it inherits. */
export interface OrgBranding {
  organization_id: number;
  /** The organization's own branding; {} when it has none. */
  settings: Partial<PersonalizationSettings>;
  updated_by: string | null;
  updated_at: string | null;
  effective: Partial<PersonalizationSettings>;
  inherits_from: Exclude<BrandingSource, "organization">;
  provider: BrandingProviderRef | null;
  followers_count: number;
  version: string;
}

/** GET /branding/deployment (Administrator). */
export interface DeploymentBranding {
  settings: Partial<PersonalizationSettings>;
  updated_by: string | null;
  updated_at: string | null;
  version: string;
}

/** GET /public/branding: the deployment default, before sign-in. */
export interface PublicBranding {
  app_name: string | null;
  tagline: string | null;
  footer_text: string | null;
  accent_color: string | null;
  theme: string | null;
  logo_light: string | null;
  logo_dark: string | null;
  favicon: string | null;
  version: string;
}
