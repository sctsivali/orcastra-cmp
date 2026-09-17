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
    // Client router cache. `dynamic` is how long an already-rendered segment is reused on a
    // return visit (Next default 0, so every back-and-forth refetched the RSC payload and
    // re-ran the auth middleware). `static` is how long a full prefetch stays reusable.
    // important: safe only because no page fetches on the server. Every app/**/page.tsx is a
    // client component or a thin shim around one, so the cached payload is chrome and never
    // cluster data, and React Query stays the single freshness authority (lib/query-config.ts).
    // If data fetching ever moves into a server component, revisit this.
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
    // Allow base64 data URLs for user-uploaded images in personalization
    dangerouslyAllowSVG: true,
    contentDispositionType: 'attachment',
    contentSecurityPolicy: "default-src 'self'; script-src 'none'; sandbox;",
  },
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,
  // Expose environment variables to the client and server
  env: {
    // NEXT_PUBLIC_API_URL is REQUIRED - set via .env or build args
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
    NEXT_PUBLIC_APP_VERSION: appVersion, // Priority: APP_VERSION env > package.json
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
          // Content-Security-Policy, staged.
          //
          // There was none at all, which matters more here than on most apps: the access token
          // is readable by client JavaScript (auth.ts), and the console renders untrusted guest
          // framebuffer and terminal output through spice-html5 and xterm. Any script that lands
          // in this origin gets an operator's bearer.
          //
          // The enforcing policy below carries only directives that cannot break a working page:
          // nothing here embeds objects, sets a <base>, is meant to be framed, or posts a form
          // off-origin. Those are real protections and they are free.
          //
          // script-src is deliberately NOT in the enforcing policy. Tightening it needs
          // per-request nonces threaded through middleware and the root layout, because Next
          // emits inline bootstrap scripts, and shipping a strict script-src blind to an app
          // whose console is xterm and SPICE is how the console breaks for operators. It is in
          // the report-only policy instead, so the violations arrive as reports and the
          // enforcing switch is flipped once they are clean rather than guessed at.
          {
            key: 'Content-Security-Policy',
            // important: NO default-src here, deliberately. default-src is the fallback for every
            // directive not listed, so `default-src 'self'` would silently become the script-src
            // and style-src too, block Next's inline bootstrap and every inline style Radix and
            // Tailwind emit, and take the whole frontend down. Listing only the directives that
            // are safe to enforce leaves the rest as unrestricted as they are today, which is
            // the definition of a change that cannot break a working page.
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
              // The shape being aimed at. Everything it would break shows up as a report first.
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
        // The basemap filename carries its own version, so a change ships as a new path and
        // the old one can be cached hard. Without this Next serves public/ with max-age=0
        // and every navigation revalidates a file that never changes.
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
  // Create Network moved from its own page into a dialog on the Networks page. The old route
  // was public for a few weeks, so a bookmark to it lands on the list rather than a 404.
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
