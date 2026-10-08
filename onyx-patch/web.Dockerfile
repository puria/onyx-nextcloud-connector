# Custom Onyx web server with the Nextcloud connector UI.
#
# Replicates the upstream web/Dockerfile from onyx v4.9.0, but sources the
# tree from a fresh clone of the pinned upstream tag with nextcloud-web.patch
# applied. Build context: onyx-patch/web/ (contains only the patch file).
#
# Build:  docker build -f onyx-patch/web.Dockerfile \
#           -t onyx-nextcloud-web:v4.9.0 onyx-patch/web/
#
# Rollback: unset ONYX_WEB_SERVER_IMAGE in the deployment .env and recreate.

# ---------------------------------------------------------------------------
# Source stage: pristine onyx v4.9.0 + the Nextcloud connector patch.
# ---------------------------------------------------------------------------
FROM alpine/git:2.49.1 AS source
ARG ONYX_SOURCE_TAG=v4.9.0
WORKDIR /repo
RUN git clone --depth 1 --branch "${ONYX_SOURCE_TAG}" \
      https://github.com/onyx-dot-app/onyx.git .
COPY nextcloud-web.patch /tmp/nextcloud-web.patch
RUN git apply --check /tmp/nextcloud-web.patch \
    && git apply /tmp/nextcloud-web.patch

# ---------------------------------------------------------------------------
# Bun binary source (mirrors upstream web/Dockerfile).
# ---------------------------------------------------------------------------
FROM oven/bun:1 AS bun_source

# ---------------------------------------------------------------------------
# Builder (mirrors upstream web/Dockerfile builder stage; build context is the
# web/ directory upstream, here supplied from the patched source stage).
# ---------------------------------------------------------------------------
FROM node:24-trixie-slim AS builder
COPY --from=bun_source /usr/local/bin/bun /usr/local/bin/bun
COPY --from=bun_source /usr/local/bin/bunx /usr/local/bin/bunx
WORKDIR /app

# Full patched web/ tree (package.json, bun.lock, tsconfig.json, lib/opal,
# lib/shared, src, public, ...).
COPY --from=source /repo/web/ ./

# opal's `prepare` hook builds dist/ during install; its tsconfig extends
# web's tsconfig.json, both present after the copy above.
RUN bun install --frozen-lockfile

ENV NEXT_PRIVATE_STANDALONE=true
ENV NEXT_TELEMETRY_DISABLED=1

ARG NEXT_PUBLIC_THEME
ENV NEXT_PUBLIC_THEME=${NEXT_PUBLIC_THEME}
ARG NEXT_PUBLIC_DISABLE_LOGOUT
ENV NEXT_PUBLIC_DISABLE_LOGOUT=${NEXT_PUBLIC_DISABLE_LOGOUT}
ARG NEXT_PUBLIC_CUSTOM_REFRESH_URL
ENV NEXT_PUBLIC_CUSTOM_REFRESH_URL=${NEXT_PUBLIC_CUSTOM_REFRESH_URL}
ARG NEXT_PUBLIC_POSTHOG_KEY
ENV NEXT_PUBLIC_POSTHOG_KEY=${NEXT_PUBLIC_POSTHOG_KEY}
ARG NEXT_PUBLIC_POSTHOG_HOST
ENV NEXT_PUBLIC_POSTHOG_HOST=${NEXT_PUBLIC_POSTHOG_HOST}
ARG NEXT_PUBLIC_CLOUD_ENABLED
ENV NEXT_PUBLIC_CLOUD_ENABLED=${NEXT_PUBLIC_CLOUD_ENABLED}
ARG NEXT_PUBLIC_SENTRY_DSN
ENV NEXT_PUBLIC_SENTRY_DSN=${NEXT_PUBLIC_SENTRY_DSN}
ARG NEXT_PUBLIC_GTM_ENABLED
ENV NEXT_PUBLIC_GTM_ENABLED=${NEXT_PUBLIC_GTM_ENABLED}
ARG NEXT_PUBLIC_FORGOT_PASSWORD_ENABLED
ENV NEXT_PUBLIC_FORGOT_PASSWORD_ENABLED=${NEXT_PUBLIC_FORGOT_PASSWORD_ENABLED}
ARG NEXT_PUBLIC_INCLUDE_ERROR_POPUP_SUPPORT_LINK
ENV NEXT_PUBLIC_INCLUDE_ERROR_POPUP_SUPPORT_LINK=${NEXT_PUBLIC_INCLUDE_ERROR_POPUP_SUPPORT_LINK}
ARG NEXT_PUBLIC_RECAPTCHA_SITE_KEY
ENV NEXT_PUBLIC_RECAPTCHA_SITE_KEY=${NEXT_PUBLIC_RECAPTCHA_SITE_KEY}
ARG NODE_OPTIONS
ENV NODE_OPTIONS=${NODE_OPTIONS}
ARG SKIP_TYPE_CHECK
ENV SKIP_TYPE_CHECK=${SKIP_TYPE_CHECK}

RUN npx next build

# ---------------------------------------------------------------------------
# Runner (mirrors upstream web/Dockerfile runner stage).
# ---------------------------------------------------------------------------
FROM node:24-trixie-slim AS runner

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

ARG WEB_FRAME_PROTECTION_ENABLED
ENV WEB_FRAME_PROTECTION_ENABLED=${WEB_FRAME_PROTECTION_ENABLED}
ARG ONYX_VERSION=0.0.0-dev
ENV ONYX_VERSION=${ONYX_VERSION}
ENV HOSTNAME="0.0.0.0"

USER node
CMD ["node", "server.js"]
