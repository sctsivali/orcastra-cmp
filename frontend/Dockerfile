# Frontend Dockerfile for Orcastra CMP
FROM node:24.21.0-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS base

# Install dependencies only when needed
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Copy package files
COPY package.json package-lock.json* yarn.lock* pnpm-lock.yaml* ./

# Install dependencies
RUN \
  if [ -f yarn.lock ]; then yarn --frozen-lockfile; \
  elif [ -f package-lock.json ]; then npm ci; \
  elif [ -f pnpm-lock.yaml ]; then corepack enable pnpm && pnpm i --frozen-lockfile; \
  else npm install; \
  fi

# Rebuild the source code only when needed
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build arguments for version management
# Empty by default, so an unstamped build reports "unknown" instead of an old release.
ARG APP_VERSION=""

# Build-time auth arguments: public values only. Secrets (CLIENT_SECRET, NEXTAUTH_SECRET) are
# injected at runtime.
ARG AUTHENTIK_ISSUER
ARG AUTHENTIK_CLIENT_ID

# NEXT_PUBLIC_* values are placeholders at build time; entrypoint.sh swaps in the real values.
ENV NEXT_PUBLIC_API_URL=http://NEXT_PUBLIC_API_URL_PLACEHOLDER
ENV NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL=http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER
# The support links use their real default as the swap pattern: an ENV set here is truthy at
# build time, so `process.env.X || DEFAULT` never falls back, and an unset runtime variable
# must still leave a working link.
ENV NEXT_PUBLIC_BUG_REPORT_URL=https://github.com/sctsivali/orcastra-cmp/issues/new
ENV NEXT_PUBLIC_COMMUNITY_URL=https://discourse.orca.id
ENV APP_VERSION=$APP_VERSION
ENV NEXT_TELEMETRY_DISABLED=1

# Auth environment variables for build (non-secret only)
ENV AUTHENTIK_ISSUER=$AUTHENTIK_ISSUER
ENV AUTHENTIK_CLIENT_ID=$AUTHENTIK_CLIENT_ID

# Build the application
RUN \
  if [ -f yarn.lock ]; then yarn build; \
  elif [ -f package-lock.json ]; then npm run build; \
  elif [ -f pnpm-lock.yaml ]; then corepack enable pnpm && pnpm run build; \
  else npm run build; \
  fi

# Production image
FROM base AS runner
WORKDIR /app

# Image labels, as in the backend image, so `docker image inspect` identifies the build.
ARG APP_VERSION=latest
ARG BUILD_COMMIT=""
ARG BUILD_DATE=""
LABEL org.opencontainers.image.title="orcastra-dashboard-frontend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Create non-root user
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy public assets
COPY --from=builder /app/public ./public

# Set the correct permission for prerender cache
RUN mkdir .next
RUN chown nextjs:nodejs .next

# Standalone output traces keep the image small
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

# Runtime env replacement script (replaces NEXT_PUBLIC_* placeholders on startup)
COPY --chown=nextjs:nodejs entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

USER nextjs

EXPOSE 2025

ENV PORT=2025
ENV HOSTNAME="0.0.0.0"

# Liveness only; keep in sync with docker-compose.prod.yml. /healthz answers from the process,
# and Node's http.get never follows the auth redirect out to the public edge.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["node", "-e", "require('http').get({host:'127.0.0.1',port:2025,path:'/healthz',timeout:5000},r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1)).on('timeout',()=>process.exit(1))"]

ENTRYPOINT ["./entrypoint.sh"]
