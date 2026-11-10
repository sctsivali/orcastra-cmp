#!/usr/bin/env node
// Branding is resolved on the server (backend services/branding/): an account sees its
// organization's own row, filled from the organization that sold to it, filled from the
// deployment default. This pins each surface to that one model: an Administrator reads the same
// path as every other role, the sign-in page and the tab carry branding, and the editor never
// previews another organization in the operator's own sidebar.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", ".."));
const read = (path) => readFileSync(join(root, "frontend/src", path), "utf8");
const code = (source) => source.split("\n").filter((line) => !line.trim().startsWith("//")).join("\n");

// 1. One read path for every role: no personal-branding branch in the loader.
const sync = code(read("components/personalization/use-personalization-sync.ts"));
assert.ok(!/orgBacked/.test(sync), "the loader still splits Administrators onto their personal row");
assert.match(sync, /readOrgPreferences\(/, "the loader reads organizations/my-preferences for everyone");

// 2. The personal row is the theme and nothing else.
const personalApi = read("lib/personalization-api.ts");
assert.match(personalApi, /body: \{ theme: ThemePreference \| null \}/, "the personal write takes only a theme");
const mutations = code(read("components/personalization/use-personalization-mutations.ts"));
assert.match(mutations, /personalizationAPI\.update\(\{ theme \}/, "the provider writes only the theme");
for (const gone of ["updateLogo", "updateColors", "updateBranding", "updateBackground"]) {
  assert.ok(!mutations.includes(gone), `the app-wide provider still edits branding (${gone})`);
}

// 3. The editor writes through its own context, and previews only the branding the sidebar shows.
const editor = read("components/personalization-settings.tsx");
assert.ok(!/usePersonalization\(\)/.test(editor), "the branding editor reads the app-wide context instead of its own");
assert.match(editor, /useBrandingEditor\(\)/, "the branding editor reads BrandingEditorContext");
const orgEditor = read("components/org-personalization-settings.tsx");
assert.match(orgEditor, /if \(previewsSidebar\) \{[\s\S]{0,200}applySettingsOptimistically/,
  "an edit to another organization must not paint the operator's own sidebar");
assert.match(orgEditor, /brandingAPI\.updateDeployment\(/, "an Administrator edits the deployment default here");

// 4. Before sign-in: the tab and the sign-in page carry the deployment default from the first byte.
const layout = read("app/layout.tsx");
assert.match(layout, /export async function generateMetadata\(\)/, "the tab title and icon are generated, not fixed");
assert.match(layout, /getPublicBranding\(\)/, "the root metadata reads the deployment default");
assert.ok(!/export const metadata\b/.test(layout), "a static metadata export would override the generated one");
assert.ok(!/<link rel="icon"/.test(layout), "hand-written icon links duplicate the generated ones");
const authLayout = read("app/auth/layout.tsx");
assert.match(authLayout, /<PublicBrandingProvider value=\{branding\}>/, "the auth pages receive the branding on the server");
const signIn = read("app/auth/signin/page.tsx");
assert.match(signIn, /<AuthBrandMark \/>/, "the sign-in page shows the deployment's mark");
assert.ok(!/>\s*Orcastra CMP\s*</.test(signIn), "the sign-in title is hard-coded again");

// 5. The public reader cannot hang the sign-in page and accepts only images the backend allows.
const reader = read("lib/server/public-branding.ts");
assert.match(reader, /Promise\.race\(\[read\(\), deadline\]\)/, "the branding read has no deadline");
assert.match(reader, /safeImageSrc\(/, "the reader passes image URLs the backend would refuse");
const images = read("lib/branding-images.ts");
assert.match(images, /endsWith\("\.cloudinary\.com"\)/, "remote images are Cloudinary only");
assert.ok(!/\\\\/.test(images.match(/const SITE_PATH = (.*);/)?.[1] ?? "\\\\"), "a site path admits no backslash");
assert.match(read("components/personalization/preferences.ts"), /safeImageSrc\(logo\.favicon\)/, "every branding image is checked where it is merged");
assert.match(read("app/page.tsx"), /cssImageUrl\(backgroundImage\)/, "the landing background is a quoted, checked url()");

// 6. Signing out forgets the previous account's colours.
assert.match(read("lib/sign-out.ts"), /clearPersonalizationHint\(\)/, "sign-out leaves the last account's colours in the hint cookie");

// 7. Signed in, the tab follows the account's organization.
assert.match(read("components/main-layout.tsx"), /<DocumentBranding \/>/, "the signed-in tab never shows the organization's branding");

console.log("branding surfaces: one read path, theme-only personal rows, scoped preview, branded sign-in and tab");
