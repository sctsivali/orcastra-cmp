import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "./globals.css";
import { Providers } from "@/components/providers";
import { LayoutProvider } from "@/components/layout-provider";
import { MainLayout } from "@/components/main-layout";
import { Toaster } from "@/components/ui/toaster";

export const metadata: Metadata = {
  title: "Orcastra Dashboard",
  description: "Enterprise operations center for managing virtualized infrastructure, containers, and cloud resources across distributed clusters",
  keywords: ["infrastructure", "operations", "dashboard", "virtualization", "containers", "cloud"],
  icons: {
    icon: [
      { url: "/favicon-light.ico", media: "(prefers-color-scheme: light)" },
      { url: "/favicon-dark.ico", media: "(prefers-color-scheme: dark)" },
    ],
    shortcut: "/favicon.ico",
    apple: "/favicon.ico",
  },
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
const personalizationInitScript = `
(function(){
  try {
    var m = document.cookie.match(/orcastra-personalization=([^;]+)/);
    if (!m) return;
    var h = JSON.parse(decodeURIComponent(m[1]));
    var r = document.documentElement;
    if (h.ac) r.style.setProperty('--personalization-accent', h.ac);
    if (h.sb) r.style.setProperty('--personalization-sidebar-bg', h.sb);
    if (h.st) r.style.setProperty('--personalization-sidebar-text', h.st);
    // compute muted text color from sidebar text
    if (h.st) {
      var hex = h.st.replace('#','');
      if (hex.length === 6) {
        var rl = parseInt(hex.substr(0,2),16)/255;
        var gl = parseInt(hex.substr(2,2),16)/255;
        var bl = parseInt(hex.substr(4,2),16)/255;
        var lum = 0.2126*(rl<=0.03928?rl/12.92:Math.pow((rl+0.055)/1.055,2.4))+0.7152*(gl<=0.03928?gl/12.92:Math.pow((gl+0.055)/1.055,2.4))+0.0722*(bl<=0.03928?bl/12.92:Math.pow((bl+0.055)/1.055,2.4));
        var muted = lum > 0.5 ? 'rgba(248,250,252,0.6)' : 'rgba(30,41,59,0.5)';
        r.style.setProperty('--personalization-sidebar-muted', muted);
      }
    }
  } catch(e) {}
})();
`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
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
        <Providers>
          <LayoutProvider>
            <MainLayout>{children}</MainLayout>
            <Toaster />
          </LayoutProvider>
        </Providers>
      </body>
    </html>
  );
}
