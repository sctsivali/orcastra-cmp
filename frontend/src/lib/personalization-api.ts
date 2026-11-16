import { fetchAPI } from "@/lib/api";
import type { ByteUnitSystem } from "@/lib/byte-units";
import type { PersonalizationSettings, ThemePreference } from "@/types/personalization";

export const personalizationAPI = {
  /** Read-only command endpoint that fetches a CORS-restricted image through the guarded backend. */
  proxyImage: (url: string): Promise<{ data: string }> =>
    fetchAPI<{ data: string }>("/user-preferences/proxy-image", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  /**
   * The account's own choices. Send either or both; a null theme clears it so the organization's
   * default applies, a null byteUnits returns to the default (binary).
   */
  update: (
    body: { theme?: ThemePreference | null; byteUnits?: ByteUnitSystem | null },
  ): Promise<PersonalizationSettings> =>
    fetchAPI<PersonalizationSettings>("/user-preferences", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
