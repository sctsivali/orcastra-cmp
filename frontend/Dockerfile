# Frontend Dockerfile for Orcastra Dashboard
FROM node:20-alpine AS base

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
ARG APP_VERSION=1.0.0-RC1

# Auth build arguments - ONLY public/non-secret values needed at build time
# Secrets (CLIENT_SECRET, NEXTAUTH_SECRET) are injected at RUNTIME via env vars
ARG AUTHENTIK_ISSUER
ARG AUTHENTIK_CLIENT_ID

# AUTH_MODE must be baked at BUILD time: it is used in `=== "client-cert"` comparisons
# that the minifier constant-folds and dead-code-eliminates, so a runtime placeholder swap
# would be too late (the wrong branch is already gone). Default "authentik" keeps the full
# image unchanged; the mini image passes --build-arg NEXT_PUBLIC_AUTH_MODE=client-cert.
ARG NEXT_PUBLIC_AUTH_MODE=authentik

# NEXT_PUBLIC_* URL vars use placeholders at build time (they are used as plain strings,
# so the runtime swap in entrypoint.sh works). Actual values injected at container startup.
ENV NEXT_PUBLIC_API_URL=http://NEXT_PUBLIC_API_URL_PLACEHOLDER
ENV NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL=http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER
ENV NEXT_PUBLIC_AUTH_MODE=$NEXT_PUBLIC_AUTH_MODE
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

# Production image, copy all the files and run next
FROM base AS runner
WORKDIR /app

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

# Automatically leverage output traces to reduce image size
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

# Runtime env replacement script (replaces NEXT_PUBLIC_* placeholders on startup)
COPY --chown=nextjs:nodejs entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

USER nextjs

EXPOSE 2025

ENV PORT=2025
ENV HOSTNAME="0.0.0.0"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:2025 || exit 1

ENTRYPOINT ["./entrypoint.sh"]
