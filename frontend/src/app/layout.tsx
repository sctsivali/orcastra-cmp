import type { Metadata, Viewport } from "next";
import { cookies } from "next/headers";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "./globals.css";
import { Providers } from "@/components/providers";
import { LayoutProvider } from "@/components/layout-provider";
import { MainLayout } from "@/components/main-layout";
import { Toaster } from "@/components/ui/toaster";
import {
  PERSONALIZATION_COOKIE,
  parsePersonalizationHint,
} from "@/lib/personalization-cookie";

export const metadata: Metadata = {
  title: "Orcastra CMP",
  description: "Open Reliable Cloud Access and Orchestration. Orcastra CMP manages virtualized infrastructure, containers, and cloud resources across distributed clusters.",
  keywords: ["cmp", "infrastructure", "operations", "dashboard", "virtualization", "containers", "cloud"],
  icons: {
    icon: [
      { url: "/favicon-light.ico", media: "(prefers-color-scheme: light)" },
      { url: "/favicon-dark.ico", media: "(prefers-color-scheme: dark)" },
    ],
    shortcut: "/favicon.ico",
    apple: "/favicon.ico",
  },
};

// The colour the browser paints its own chrome with: the mobile address bar, the pull-to-refresh
// overshoot and the PWA splash. Without it those stay the UA default and frame a #14111c page in
// white. Keyed to the OS preference rather than the stored theme because the browser reads this
// before any of our script runs; the values are the two --background tokens in globals.css.
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3f4f6" },
    { media: "(prefers-color-scheme: dark)", color: "#14111c" },
  ],
};

// inline script to prevent theme flash (FOUC) on page load
// reads cookie set by ThemeProvider, falls back to system preference
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

// inline script to prevent personalization flash (FOUC) on page load
// reads compact cookie with visual CSS values and applies them before paint
//
// Returning early on a missing cookie is deliberate: with no hint to apply, the seeds in
// globals.css are already the shipped defaults and touching anything here would only risk
// writing something worse.
//
// important: the accent is written twice, as a hex AND as the h/s/l triplet that
// globals.css binds --primary and --ring to. Writing only the hex left every Button, Badge,
// switch, checkbox and focus ring on the seed accent until React mounted, so an operator with
// a custom accent watched the whole app change colour one tick after paint. The triplet is
// what the stylesheet actually reads.
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
    // Same rounding as hexToHSL in components/personalization-provider.tsx, so the pre-paint
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
    if (h.ac) {
      r.style.setProperty('--personalization-accent', h.ac);
      var ac = rgb(h.ac);
      if (ac) {
        var a = hsl(ac);
        r.style.setProperty('--personalization-accent-h', String(a[0]));
        r.style.setProperty('--personalization-accent-s', a[1] + '%');
        r.style.setProperty('--personalization-accent-l', a[2] + '%');
      }
    }
    if (h.sb) r.style.setProperty('--personalization-sidebar-bg', h.sb);
    if (h.st) {
      r.style.setProperty('--personalization-sidebar-text', h.st);
      var st = rgb(h.st);
      if (st) {
        r.style.setProperty(
          '--personalization-sidebar-muted',
          lum(st) > 0.5 ? 'rgba(248,250,252,0.6)' : 'rgba(30,41,59,0.5)'
        );
      }
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
  const cookieStore = await cookies();
  const sidebarCollapsed = cookieStore.get("orcastra-sidebar-collapsed")?.value === "true";
  // Same reasoning one property further: the sidebar's colour has to be right in the first
  // byte too. The pre-paint script below can set the CSS variables from this cookie, but it
  // cannot decide whether the aside carries `sidebar-custom-bg` at all, because that is a
  // className React owns. Reading the hint here is what keeps the server render and the
  // browser's first render describing the same sidebar.
  const initialPersonalization = parsePersonalizationHint(
    cookieStore.get(PERSONALIZATION_COOKIE)?.value,
  );

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Prevent theme flash: apply theme before paint via cookie hint */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
        {/* Prevent personalization flash: apply CSS custom properties before paint */}
        <script dangerouslySetInnerHTML={{ __html: personalizationInitScript }} />
        {/* Dynamic favicon based on system color scheme */}
        <link rel="icon" href="/favicon-light.ico" type="image/x-icon" media="(prefers-color-scheme: light)" />
        <link rel="icon" href="/favicon-dark.ico" type="image/x-icon" media="(prefers-color-scheme: dark)" />
        <link rel="shortcut icon" href="/favicon.ico" type="image/x-icon" />
      </head>
      <body
        className={`${GeistSans.variable} ${GeistMono.variable} font-sans antialiased min-h-screen bg-background`}
      >
        <Providers initialPersonalization={initialPersonalization}>
          <LayoutProvider initialCollapsed={sidebarCollapsed}>
            <MainLayout>{children}</MainLayout>
            <Toaster />
          </LayoutProvider>
        </Providers>
      </body>
    </html>
  );
}
