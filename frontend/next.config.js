/** @type {import('next').NextConfig} */

// Read version from package.json as fallback
const packageJson = require('./package.json');

// Prefer APP_VERSION from environment (set via Docker build args or .env), fallback to package.json
const appVersion = process.env.APP_VERSION || packageJson.version;

const nextConfig = {
  output: 'standalone',
  // Tree-shake large packages for smaller bundles
  experimental: {
    optimizePackageImports: [
      'lucide-react',
      'recharts',
      '@radix-ui/react-icons',
      'date-fns',
    ],
    // Client router cache. `dynamic` is how long a rendered segment is reused on a return visit
    // (Next's default 0 refetches the RSC payload and re-runs the auth middleware every time).
    // `static` is how long a full prefetch stays reusable.
    // important: safe only while no page fetches on the server. Every app/**/page.tsx is a client
    // component or a thin shim around one, so the cached payload never holds cluster data and
    // React Query stays the freshness authority (lib/query-config.ts). The one server read, public
    // branding (lib/server/public-branding.ts), is the same for every viewer and cached for 60 s.
    // Revisit this if any other data fetching moves into a server component.
    staleTimes: {
      dynamic: 30,
      static: 180,
    },
  },
  images: {
    remotePatterns: [{
      protocol: 'https',
      hostname: '**.cloudinary.com',
      port: '',
    }],
    formats: ['image/avif', 'image/webp'],
    // Lets next/image serve SVG; the attachment disposition and sandboxed CSP below keep an SVG
    // from running script.
    dangerouslyAllowSVG: true,
    contentDispositionType: 'attachment',
    contentSecurityPolicy: "default-src 'self'; script-src 'none'; sandbox;",
  },
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,
  // Expose environment variables to the client and server
  env: {
    // Required; set through .env or build args.
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
    NEXT_PUBLIC_APP_VERSION: appVersion,
  },
  // Server-side environment variables (not exposed to client)
  // These are automatically available via process.env on the server
  serverRuntimeConfig: {
    AUTHENTIK_ISSUER: process.env.AUTHENTIK_ISSUER,
    AUTHENTIK_CLIENT_ID: process.env.AUTHENTIK_CLIENT_ID,
    AUTHENTIK_CLIENT_SECRET: process.env.AUTHENTIK_CLIENT_SECRET,
    NEXTAUTH_URL: process.env.NEXTAUTH_URL,
    NEXTAUTH_SECRET: process.env.NEXTAUTH_SECRET,
  },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          {
            key: 'X-DNS-Prefetch-Control',
            value: 'on'
          },
          {
            key: 'Strict-Transport-Security',
            value: 'max-age=63072000; includeSubDomains; preload'
          },
          {
            key: 'X-Frame-Options',
            value: 'SAMEORIGIN'
          },
          {
            key: 'X-Content-Type-Options',
            value: 'nosniff'
          },
          {
            key: 'X-XSS-Protection',
            value: '1; mode=block'
          },
          {
            key: 'Referrer-Policy',
            value: 'origin-when-cross-origin'
          },
          {
            key: 'Permissions-Policy',
            value: 'camera=(), microphone=(), geolocation=()'
          },
          // Content-Security-Policy, staged. The access token is readable by client JavaScript
          // (auth.ts) and the console renders untrusted guest framebuffer and terminal output
          // through spice-html5 and xterm, so any script that lands in this origin gets an
          // operator's bearer.
          //
          // The enforcing policy carries only directives that cannot break a working page: nothing
          // here embeds objects, sets a <base>, is meant to be framed, or posts a form off-origin.
          //
          // script-src is report-only for now. Next emits inline bootstrap scripts, so enforcing it
          // needs per-request nonces threaded through middleware and the root layout; the reports
          // show what a strict policy would break in the xterm and SPICE consoles first.
          {
            key: 'Content-Security-Policy',
            // important: no default-src here. It is the fallback for every directive not listed,
            // so `default-src 'self'` would silently become script-src and style-src too, block
            // Next's inline bootstrap and every inline style Radix and Tailwind emit, and take the
            // whole frontend down.
            value: [
              "object-src 'none'",
              "base-uri 'self'",
              "form-action 'self'",
              "frame-ancestors 'self'",
            ].join('; '),
          },
          {
            key: 'Content-Security-Policy-Report-Only',
            value: [
              "default-src 'self'",
              // The target policy. Everything it would break shows up as a report first.
              "script-src 'self'",
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
              "img-src 'self' data: blob: https://*.cloudinary.com",
              "font-src 'self' data:",
              // Same-origin API plus the WebSocket the console, terminal and event stream use.
              "connect-src 'self' ws: wss:",
              "object-src 'none'",
              "base-uri 'self'",
              "form-action 'self'",
              "frame-ancestors 'self'",
            ].join('; '),
          }
        ],
      },
      {
        // important: guest file bytes. This must follow the rule above, because for one header key
        // the last match wins, and that rule would otherwise replace the backend's sandbox with
        // the app's policy. The access token is readable by script, so a guest's HTML or SVG must
        // never run on this origin, even if a browser ignored `attachment`.
        source: '/api/v1/instance-files/download',
        headers: [
          { key: 'Content-Security-Policy', value: "sandbox; default-src 'none'" },
          { key: 'Referrer-Policy', value: 'no-referrer' },
        ],
      },
      {
        // The basemap filename carries its own version, so a change ships as a new path and
        // each path can be cached hard. Without this Next serves public/ with max-age=0 and
        // every navigation revalidates a file that never changes.
        source: '/geo/:path*',
        headers: [
          {
            key: 'Cache-Control',
            value: 'public, max-age=31536000, immutable'
          }
        ],
      },
    ];
  },
  async rewrites() {
    return {
      beforeFiles: [],
      afterFiles: [],
      fallback: [],
    };
  },
  // Create Network is a dialog on the Networks page, so a bookmarked create URL lands on the list
  // instead of a 404.
  async redirects() {
    return [
      {
        source: '/inventory/networks/create',
        destination: '/inventory/networks',
        permanent: false,
      },
    ];
  },
};

module.exports = nextConfig;
