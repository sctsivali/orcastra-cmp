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
  description: "Unified dashboard for managing LXD virtual machines across multiple nodes",
  keywords: ["LXD", "virtualization", "dashboard", "containers", "VMs"],
  icons: {
    icon: [
      { url: "/favicon-light.ico", media: "(prefers-color-scheme: light)" },
      { url: "/favicon-dark.ico", media: "(prefers-color-scheme: dark)" },
    ],
    shortcut: "/favicon.ico",
    apple: "/favicon.ico",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
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
