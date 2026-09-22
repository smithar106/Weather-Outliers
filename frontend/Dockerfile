# Frontend image: the public website.
#
# Build context is THIS directory, not the repository root:
#
#   cd frontend && docker build -t weather-outliers-frontend .
#
# Self-contained on purpose. A PaaS builder detects a Dockerfile at the root of
# the build context and otherwise guesses from the languages it finds, which for
# this repository's root — a Python service beside a Node service — it cannot do.
# Pointing a service's root directory at frontend/ makes this file the one that is
# found, with no builder configuration to set. See docs/deployment.md.
#
# The cost of the narrower context: this image cannot copy evals/reports/latest.json
# from above itself, so the prebuild step uses the committed copy at
# src/data/evaluation-report.json. That copy is not allowed to drift — CI runs
# `npm run check-evals`, which fails if it differs from evals/reports/latest.json.
# The guarantee moved from the image to the pipeline, which is the better place for
# it: it now fails a pull request rather than a deploy.

# ---------------------------------------------------------------- dependencies
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# `npm ci` and not `npm install`: the lockfile is the build input. An install that
# is allowed to resolve a different tree is an image that does not match CI.
RUN npm ci

# --------------------------------------------------------------------- builder
FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . ./

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
# no node_modules of its own. `.next/static` is copied separately because the
# standalone output does not include it. There is no `public/` line below because
# this site has no public/ directory — every asset is either inlined or served
# from .next/static. If one is ever added, it needs its own COPY here, and it
# needs a tracked file inside it, because git does not store empty directories
# and COPY of a missing path fails the build.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 3000

# server.js honours PORT and HOSTNAME. HOSTNAME must be 0.0.0.0 or the container
# will only answer itself.
ENV HOSTNAME=0.0.0.0
CMD ["sh", "-c", "PORT=${PORT:-3000} node server.js"]
