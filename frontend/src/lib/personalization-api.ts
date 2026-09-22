import { fetchAPI } from "@/lib/api";
import type { PersonalizationSettings } from "@/types/personalization";

export const personalizationAPI = {
  /** Read-only command endpoint used to fetch a CORS-restricted image through the guarded backend. */
  proxyImage: (url: string, accessToken?: string): Promise<{ data: string }> =>
    fetchAPI<{ data: string }>("/user-preferences/proxy-image", {
      method: "POST",
      body: JSON.stringify({ url }),
      ...(accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : {}),
    }),

  update: (
    settings: PersonalizationSettings,
    accessToken?: string,
  ): Promise<PersonalizationSettings> =>
    fetchAPI<PersonalizationSettings>("/user-preferences", {
      method: "PUT",
      body: JSON.stringify(settings),
      ...(accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : {}),
    }),

  delete: (accessToken?: string): Promise<void> =>
    fetchAPI<void>("/user-preferences", {
      method: "DELETE",
      ...(accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : {}),
    }),
};
