# Frontend image: the public website.
#
# Build context is the repository root so that `evals/reports/latest.json` is
# visible to the prebuild step. It is optional — `sync-evaluation-report.mjs`
# falls back to the committed copy in `src/data/` — but when it is present the
# deployed page shows the report belonging to the deployed commit.
#
#   docker build -f frontend/Dockerfile -t weather-outliers-frontend .

# ---------------------------------------------------------------- dependencies
FROM node:22-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` and not `npm install`: the lockfile is the build input. An install that
# is allowed to resolve a different tree is an image that does not match CI.
RUN npm ci

# --------------------------------------------------------------------- builder
FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ ./
# Optional, and placed where the prebuild script looks for it: that script resolves
# the report relative to itself, two levels up from frontend/scripts/, which inside
# this stage is /evals/reports/. Absent, it falls back to the committed copy.
COPY evals/reports/ /evals/reports/

# The build must not reach the backend. Every data page is `force-dynamic`, so
# nothing is prerendered and no API_BASE_URL is needed here — a build that could
# fail because a database was asleep is a build that will.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# --------------------------------------------------------------------- runtime
FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# `output: "standalone"` traces the modules actually reached, so the runtime has
# no node_modules of its own. Static assets and the public directory are copied
# separately because the standalone server does not include them.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 3000

# server.js honours PORT and HOSTNAME. HOSTNAME must be 0.0.0.0 or the container
# will only answer itself.
ENV HOSTNAME=0.0.0.0
CMD ["sh", "-c", "PORT=${PORT:-3000} node server.js"]
