# Backend image: serves the read-only public API, and is also the image the
# scheduled worker runs `python -m app.pipeline` inside. One image for both is
# deliberate — the pipeline and the API must agree about the methodology version
# and the schema, and separate images are how they drift apart.
#
#   docker build -t weather-outliers-backend .
#
# Why this file is at the repository root rather than in backend/, which is where
# it belongs on the face of it:
#
#   1. It needs a root build context regardless, because the city registry lives
#      in `data/` outside backend/ and both the API and the pipeline read it.
#   2. PaaS builders detect a Dockerfile at the root of the build context and
#      otherwise fall back to language autodetection, which cannot make sense of a
#      repository whose root holds a Python service and a Node service side by
#      side. Three Railway deploys failed on exactly that before this moved here.
#      See docs/deployment.md.
#
# The website has its own self-contained image in frontend/, built with frontend/
# as its context. `evals/` is not copied into either: nothing in `app/` reads it.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg[binary] ships its own libpq, so no build toolchain and no libpq-dev are
# needed here. Kept in mind rather than discovered: adding a dependency that
# needs compiling means adding a builder stage, not apt-get to this one.

# Dependencies first, so a code change does not reinstall them.
#
# setuptools will not build a project whose package directory is absent, and the
# code is copied *after* this layer precisely so that editing it does not reinstall
# every dependency. So the package is stubbed for the length of the install and then
# removed: `app` is an implicit namespace package in this repository — there is no
# backend/app/__init__.py to copy — and leaving a stub behind would make the image's
# import semantics differ from the one the tests run against.
COPY backend/pyproject.toml ./pyproject.toml
RUN mkdir -p app \
    && touch app/__init__.py \
    && pip install --no-cache-dir . \
    && rm app/__init__.py

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
# The city registry is data the application reads at seed time, not a fixture.
COPY data ./data

# Run as a non-root user. The container writes nothing outside /tmp.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# PORT is injected by most platforms, including Railway; 8000 is the local default.
# One worker: the pipeline is a separate process and the API is I/O-bound against
# PostgreSQL, so process count is a deployment knob rather than a Dockerfile one.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
