import { fetchAPI } from "@/lib/api";
import type { IllustrationPreference, PersonalizationSettings, ThemePreference } from "@/types/personalization";

export const personalizationAPI = {
  /** Read-only command endpoint used to fetch a CORS-restricted image through the guarded backend. */
  proxyImage: (url: string, accessToken?: string): Promise<{ data: string }> =>
    fetchAPI<{ data: string }>("/user-preferences/proxy-image", {
      method: "POST",
      body: JSON.stringify({ url }),
      ...(accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : {}),
    }),

  /** The account's own theme and illustrations choice. Send either; null clears that one so the
   *  organization's default applies, and a key left out is kept as it is. */
  update: (
    body: { theme?: ThemePreference | null; illustrations?: IllustrationPreference | null },
    accessToken?: string,
  ): Promise<PersonalizationSettings> =>
    fetchAPI<PersonalizationSettings>("/user-preferences", {
      method: "PUT",
      body: JSON.stringify(body),
      ...(accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : {}),
    }),
};
