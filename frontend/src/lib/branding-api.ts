import { fetchAPI } from "@/lib/api";
import type { DeploymentBranding } from "@/types/branding";
import type { PersonalizationSettings } from "@/types/personalization";

/** The deployment default branding (Administrator). Organizations' rows are on api.organizations. */
export const brandingAPI = {
  getDeployment: () => fetchAPI<DeploymentBranding>("/branding/deployment"),
  updateDeployment: (settings: PersonalizationSettings) =>
    fetchAPI<DeploymentBranding>("/branding/deployment", {
      method: "PUT",
      body: JSON.stringify({ settings }),
    }),
  deleteDeployment: () => fetchAPI<void>("/branding/deployment", { method: "DELETE" }),
};
