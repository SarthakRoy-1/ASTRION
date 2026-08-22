# ParcelPilot frontend — Next.js chat UI.
#
# Build context is app/frontend/ itself (self-contained; no dependency on the
# rest of the repo). Build from that directory, or from the repo root with
# `-f app/frontend/Dockerfile app/frontend`:
#
#   docker build --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com \
#       -t parcelpilot-frontend app/frontend
#
# IMPORTANT — NEXT_PUBLIC_API_BASE_URL is a BUILD-TIME value, not a runtime
# one. Next.js inlines every NEXT_PUBLIC_* variable into the compiled client
# JavaScript at `next build`; setting it with `docker run -e` on an
# already-built image has no effect on the browser bundle; that request
# checked, no undo is possible short of rebuilding. Rebuild the image for
# each backend URL you deploy against — there is no runtime substitution here.

FROM node:24-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-slim AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:24-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
# The node:24-slim base image already ships a non-root `node` user at
# UID/GID 1000 — reused rather than creating a second one, which fails
# because that uid is already taken.
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/next.config.ts ./
COPY --from=build /app/public ./public
COPY --from=build /app/.next ./.next
COPY --from=deps /app/node_modules ./node_modules
USER node

EXPOSE 3000
CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
