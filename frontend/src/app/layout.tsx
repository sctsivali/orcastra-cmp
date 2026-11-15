import type { Metadata, Viewport } from "next";
import { cookies, headers } from "next/headers";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "./globals.css";
import "@/components/illustrations/illustrations.css";
import { Providers } from "@/components/providers";
import { LayoutProvider } from "@/components/layout-provider";
import { MainLayout } from "@/components/main-layout";
import { Toaster } from "@/components/ui/toaster";
import {
  PERSONALIZATION_COOKIE,
  parseByteUnitsHint,
  parsePersonalizationHint,
} from "@/lib/personalization-cookie";
import { DEFAULT_PERSONALIZATION } from "@/types/personalization";
import { DOCK_COOKIE, parseDockCookie } from "@/lib/dock-cookie";
import { readHydratableSession } from "@/auth-session";
import { getPublicBranding } from "@/lib/server/public-branding";

const DEFAULT_TITLE = "Orcastra CMP";
const DEFAULT_ICONS: Metadata["icons"] = {
  icon: [
    { url: "/favicon-light.ico", media: "(prefers-color-scheme: light)" },
    { url: "/favicon-dark.ico", media: "(prefers-color-scheme: dark)" },
  ],
  shortcut: "/favicon.ico",
  apple: "/favicon.ico",
};

// The tab title and icon before sign-in come from the deployment default branding an
// Administrator set (Settings > Personalization > Deployment Default). Signed in, an account's
// organization branding takes over (components/branding/document-branding.tsx).
export async function generateMetadata(): Promise<Metadata> {
  const branding = await getPublicBranding();
  return {
    title: branding?.app_name ?? DEFAULT_TITLE,
    description: "Open Reliable Cloud Access and Orchestration. Orcastra CMP manages virtualized infrastructure, containers, and cloud resources across distributed clusters.",
    keywords: ["cmp", "infrastructure", "operations", "dashboard", "virtualization", "containers", "cloud"],
    icons: branding?.favicon
      ? { icon: branding.favicon, shortcut: branding.favicon, apple: branding.favicon }
      : DEFAULT_ICONS,
  };
}

// The colour the browser paints its own chrome with: the mobile address bar, the pull-to-refresh
// overshoot and the PWA splash. Without it those stay the UA default and frame a #14111c page in
// white. Keyed to the OS preference rather than the stored theme because the browser reads this
// before any page script runs; the values are the two --background tokens in globals.css.
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3f4f6" },
    { media: "(prefers-color-scheme: dark)", color: "#14111c" },
  ],
};

// Applies the theme before paint from the cookie ThemeProvider sets, falling back to the system
// preference, so the page never flashes the wrong theme.
const themeInitScript = `
(function(){
  try {
    var c = document.cookie.match(/orcastra-theme=([^;]+)/);
    var t = c ? c[1] : 'system';
    var resolved = t;
    if (t === 'system') {
      resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    var root = document.documentElement;
    root.classList.remove('light', 'dark');
    root.classList.add(resolved);
  } catch(e) {}
})();
`;

// Applies the personalization cookie's CSS values before paint. Without the cookie it does
// nothing: the globals.css seeds are already the defaults.
//
// important: the accent is written as a hex and as the h/s/l triplet that globals.css binds
// --primary and --ring to. The stylesheet reads the triplet; with only the hex, every control
// and focus ring stays on the seed accent until React mounts.
const personalizationInitScript = `
(function(){
  try {
    var m = document.cookie.match(/orcastra-personalization=([^;]+)/);
    if (!m) return;
    var h = JSON.parse(decodeURIComponent(m[1]));
    var r = document.documentElement;
    var rgb = function(x){
      if (typeof x !== 'string') return null;
      var v = x.replace('#','');
      if (!/^[0-9a-fA-F]{6}$/.test(v)) return null;
      return [parseInt(v.substr(0,2),16)/255, parseInt(v.substr(2,2),16)/255, parseInt(v.substr(4,2),16)/255];
    };
    // Same rounding as hexToHSL in lib/color.ts, so the pre-paint
    // value and the hydrated value are identical rather than merely close.
    var hsl = function(c){
      var mx = Math.max(c[0],c[1],c[2]), mn = Math.min(c[0],c[1],c[2]), l = (mx+mn)/2, hh = 0, ss = 0;
      if (mx !== mn) {
        var d = mx-mn;
        ss = l > 0.5 ? d/(2-mx-mn) : d/(mx+mn);
        if (mx === c[0]) hh = ((c[1]-c[2])/d + (c[1] < c[2] ? 6 : 0))/6;
        else if (mx === c[1]) hh = ((c[2]-c[0])/d + 2)/6;
        else hh = ((c[0]-c[1])/d + 4)/6;
      }
      return [Math.round(hh*360), Math.round(ss*100), Math.round(l*100)];
    };
    var lum = function(c){
      var f = function(v){ return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
      return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]);
    };
    var ratio = function(x, y){ var a = lum(x), b = lum(y); return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05); };
    var ac = rgb(h.ac);
    if (ac) {
      r.style.setProperty('--personalization-accent', h.ac);
      var a = hsl(ac);
      r.style.setProperty('--personalization-accent-h', String(a[0]));
      r.style.setProperty('--personalization-accent-s', a[1] + '%');
      r.style.setProperty('--personalization-accent-l', a[2] + '%');
      // The accent's own text (personalization-colors.accentForeground) and the hue brand
      // artwork turns by (accentHueShift), as the provider sets them after hydration.
      var dark = rgb('#1e293b');
      var fg = ratio(dark, ac) > ratio([1,1,1], ac) ? '#1e293b' : '#ffffff';
      var f = hsl(rgb(fg));
      r.style.setProperty('--personalization-accent-foreground', fg);
      r.style.setProperty('--primary-foreground', f[0] + ' ' + f[1] + '% ' + f[2] + '%');
      var shift = a[0] - hsl(rgb('#8467e4'))[0];
      if (shift > 180) shift -= 360;
      if (shift <= -180) shift += 360;
      r.style.setProperty('--personalization-accent-hue-shift', shift + 'deg');
    }
    var sb = rgb(h.sb);
    if (sb) r.style.setProperty('--personalization-sidebar-bg', h.sb);
    // The text the sidebar renders: auto picks from the ground (resolvedSidebarText), so a light
    // ground does not paint white text until React mounts.
    var text = (h.atc !== false && !h.ust && sb) ? (lum(sb) < 0.5 ? '#f8fafc' : '#1e293b') : h.st;
    var st = rgb(text);
    if (st) {
      r.style.setProperty('--personalization-sidebar-text', text);
      r.style.setProperty(
        '--personalization-sidebar-muted',
        lum(st) > 0.5 ? 'rgba(248,250,252,0.6)' : 'rgba(30,41,59,0.5)'
      );
    }
  } catch(e) {}
})();
`;

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Read the collapse preference on the server so the sidebar width and the content
  // margin are correct in the first byte of HTML. Absent cookie means expanded.
  // The session is read alongside so the first client render already knows its account and
  // fires its data requests without waiting on /api/auth/session (auth-session.ts).
  const [cookieStore, initialSession, requestHeaders] = await Promise.all([cookies(), readHydratableSession(), headers()]);
  // This response's Content Security Policy nonce (middleware.ts, lib/csp.ts). Without it the
  // browser blocks the two inline scripts below once script-src is enforced.
  const nonce = requestHeaders.get("x-nonce") ?? undefined;
  const sidebarCollapsed = cookieStore.get("orcastra-sidebar-collapsed")?.value === "true";
  // The task panel's open state and height, for the same first-byte reason.
  const initialDock = parseDockCookie(cookieStore.get(DOCK_COOKIE)?.value);
  // The sidebar colour has to be right in the first byte too. The pre-paint script can set the
  // CSS variables, but whether the aside carries `sidebar-custom-bg` is a className React owns;
  // reading the hint here keeps the server render and the first client render in agreement.
  const personalizationHint = cookieStore.get(PERSONALIZATION_COOKIE)?.value;
  const initialPersonalization = parsePersonalizationHint(personalizationHint);
  // Sizes print on the server too; the hint keeps that text and the first client render alike.
  const initialByteUnits = parseByteUnitsHint(personalizationHint);
  // Card illustrations from the first byte: the account's hint when there is one, otherwise the
  // deployment's choice, which the sign-in pages show before anyone signs in. getPublicBranding
  // is the same memoised read generateMetadata made, so this adds no request.
  const illustrations =
    initialPersonalization?.illustrations ??
    (await getPublicBranding())?.illustrations ??
    DEFAULT_PERSONALIZATION.illustrations;

  return (
    <html
      lang="en"
      data-illustrations={illustrations}
      suppressHydrationWarning
    >
      <head>
        {/* Prevent theme flash: apply theme before paint via cookie hint */}
        <script nonce={nonce} suppressHydrationWarning dangerouslySetInnerHTML={{ __html: themeInitScript }} />
        {/* Prevent personalization flash: apply CSS custom properties before paint */}
        <script nonce={nonce} suppressHydrationWarning dangerouslySetInnerHTML={{ __html: personalizationInitScript }} />
        {/* The favicon links come from generateMetadata above; do not write them here too. */}
      </head>
      <body
        className={`${GeistSans.variable} ${GeistMono.variable} font-sans antialiased min-h-screen bg-background`}
      >
        <Providers initialPersonalization={initialPersonalization} initialByteUnits={initialByteUnits} initialSession={initialSession}>
          <LayoutProvider initialCollapsed={sidebarCollapsed} initialDock={initialDock}>
            <MainLayout>{children}</MainLayout>
            <Toaster />
          </LayoutProvider>
        </Providers>
      </body>
    </html>
  );
}
