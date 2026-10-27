import { fetchAPI } from "@/lib/api";
import type { PersonalizationSettings, ThemePreference } from "@/types/personalization";

export const personalizationAPI = {
  /** Read-only command endpoint used to fetch a CORS-restricted image through the guarded backend. */
  proxyImage: (url: string, accessToken?: string): Promise<{ data: string }> =>
    fetchAPI<{ data: string }>("/user-preferences/proxy-image", {
      method: "POST",
      body: JSON.stringify({ url }),
      ...(accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : {}),
    }),

  /** The account's own theme; null clears it so the organization's default applies. */
  update: (
    body: { theme: ThemePreference | null },
    accessToken?: string,
  ): Promise<PersonalizationSettings> =>
    fetchAPI<PersonalizationSettings>("/user-preferences", {
      method: "PUT",
      body: JSON.stringify(body),
      ...(accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : {}),
    }),
};
