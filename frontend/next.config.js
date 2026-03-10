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
          }
        ],
      },
    ];
  },
  async rewrites() {
    return {
      // beforeFiles rewrites are checked before pages/public files
      // allowing API routes to be handled by Next.js first
      beforeFiles: [],
      // afterFiles rewrites are checked after pages/public files
      // but before fallback routes  
      afterFiles: [
        {
          // Rewrite /api/v1/* to backend (main API routes)
          // NEXT_PUBLIC_API_URL is REQUIRED - set via .env or build args
          source: '/api/v1/:path*',
          destination: `${process.env.NEXT_PUBLIC_API_URL}/api/v1/:path*`,
        },
      ],
      // Fallback rewrites - /api/auth/* is handled by NextAuth locally
      fallback: [],
    };
  },
};

module.exports = nextConfig;
