FROM node:22-bookworm-slim AS deps
WORKDIR /app
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:22-bookworm-slim AS build
WORKDIR /app
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm run build

FROM node:22-bookworm-slim AS run
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
# The runner is a non-root user, and the panel holds no writable state: the
# broker owns the queue and the session is a signed cookie.
RUN groupadd --system --gid 10001 cindral \
 && useradd --system --uid 10001 --gid cindral --no-create-home cindral
COPY --from=build --chown=cindral:cindral /app/.next/standalone ./
COPY --from=build --chown=cindral:cindral /app/.next/static ./.next/static
USER cindral
EXPOSE 3000
CMD ["node", "server.js"]
